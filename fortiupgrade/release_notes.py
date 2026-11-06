"""Configuration-vs-release-notes analysis.

Two layers:
  * rules.yaml  - curated, version-gated rules evaluated against the running config.
  * release notes text - the user supplies release-notes PDFs/HTML/text for each hop;
    we inventory the features the config uses and surface every Known-issue /
    Special-notice / Changed-behaviour paragraph that mentions one of them.
"""
from __future__ import annotations

import html
import io
import re
from pathlib import Path

import httpx
import yaml

from .version import FosVersion

DATA = Path(__file__).parent / "data"

# config section prefix -> human feature terms searched for in release notes
FEATURES: dict[str, list[str]] = {
    "config router bgp": ["BGP"],
    "config router ospf": ["OSPF"],
    "config router ospf6": ["OSPFv3", "OSPF6"],
    "config router bfd": ["BFD"],
    "config router policy": ["policy route", "policy-based routing", "PBR"],
    "config router static": ["static route"],
    "config router multicast": ["multicast", "PIM"],
    "config vpn ipsec": ["IPsec"],
    "config vpn ssl": ["SSL VPN", "SSL-VPN", "Agentless VPN"],
    "config system sdwan": ["SD-WAN"],
    "config system virtual-wan-link": ["SD-WAN", "virtual-wan-link"],
    "config system ha": ["HA ", "FGCP", "high availability", "cluster"],
    "config system standalone-cluster": ["FGSP", "session sync"],
    "config system cluster-sync": ["FGSP", "session sync"],
    "config web-proxy": ["explicit proxy", "web proxy", "web-proxy"],
    "config wanopt": ["WAN optimization", "WAN opt"],
    "config user ldap": ["LDAP"],
    "config user radius": ["RADIUS"],
    "config user saml": ["SAML"],
    "config user fsso": ["FSSO"],
    "config firewall access-proxy": ["ZTNA", "access proxy"],
    "config system ztna": ["ZTNA"],
    "config switch-controller": ["FortiSwitch", "FortiLink"],
    "config wireless-controller": ["FortiAP", "wireless"],
    "config system vxlan": ["VXLAN"],
    "config system geneve": ["GENEVE"],
    "config firewall shaping": ["traffic shaping", "shaper"],
    "config ips sensor": ["IPS"],
    "config antivirus profile": ["antivirus", "AV profile"],
    "config dnsfilter": ["DNS filter"],
    "config webfilter": ["web filter", "webfilter"],
    "config application list": ["application control"],
    "config firewall vip": ["VIP", "virtual IP"],
    "config firewall ippool": ["IP pool"],
    "config system automation": ["automation stitch", "automation"],
    "config log fortianalyzer": ["FortiAnalyzer"],
    "config system central-management": ["FortiManager"],
    "config system csf": ["Security Fabric"],
    "config system dhcp server": ["DHCP server"],
    "config system npu": ["NP6", "NP7", "NPU", "offload"],
    "config system lte-modem": ["LTE", "modem"],
    "config firewall local-in-policy": ["local-in policy", "local-in"],
    "config system saml": ["SAML"],
    "config system interface": ["aggregate", "LACP", "VLAN", "EMAC"],
    "config system virtual-switch": ["virtual switch", "hardware switch"],
}

SECTION_HEADINGS = [
    "special notices", "changes in cli", "changes in default behavior", "changes in default values",
    "changes in gui behavior", "changes in table size", "new features", "upgrade information",
    "product integration", "resolved issues", "known issues", "limitations", "built-in", "deprecated",
    "removed", "change log", "introduction",
]
INTERESTING = ("special notices", "changes in", "upgrade information", "known issues", "limitations",
               "deprecated", "removed")


# -------------------------------------------------------------------- rules
def load_rules(extra: Path | None = None) -> list[dict]:
    rules = {}
    for f in [DATA / "rules.yaml", extra]:
        if f and Path(f).exists():
            for r in (yaml.safe_load(Path(f).read_text()) or {}).get("rules", []):
                rules[r["id"]] = r
    return list(rules.values())


def evaluate_rules(config: str, current: str, target: str, model: str = "",
                   extra_rules: Path | None = None) -> list[dict]:
    cur, tgt = FosVersion.parse(current), FosVersion.parse(target)
    hits = []
    for r in load_rules(extra_rules):
        intro = r.get("introduced_in")
        if intro and not (cur < FosVersion.parse(intro) <= tgt):
            continue
        if r.get("model_regex") and not re.search(r["model_regex"], model or "", re.I):
            continue
        all_ok = all(re.search(p, config, re.M) for p in r.get("config_all", []))
        anys = r.get("config_any")
        any_ok = True if not anys else any(re.search(p, config, re.M) for p in anys)
        if all_ok and any_ok:
            evidence = []
            for p in (r.get("config_all", []) + (anys or [])):
                m = re.search(p, config, re.M)
                if m:
                    evidence.append(m.group(0).strip().splitlines()[0][:160])
            hits.append({"id": r["id"], "title": r["title"], "severity": r.get("severity", "warning"),
                         "introduced_in": intro, "message": " ".join(str(r.get("message", "")).split()),
                         "reference": r.get("reference", ""), "evidence": evidence[:4]})
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(hits, key=lambda h: order.get(h["severity"], 3))


# ---------------------------------------------------------------- features
def feature_inventory(config: str) -> dict[str, list[str]]:
    found = {}
    for prefix, terms in FEATURES.items():
        if re.search(r"^\s*" + re.escape(prefix) + r"\b", config, re.M):
            # treat as used only if the block has some content
            m = re.search(r"^\s*" + re.escape(prefix) + r".*\n\s*(edit|set) ", config, re.M)
            if m:
                found[prefix] = terms
    # refine features that exist in every configuration
    if "config system ha" in found and not re.search(r"^\s*set mode (a-p|a-a)\b", config, re.M):
        found.pop("config system ha")
    if "config system interface" in found:
        found.pop("config system interface")
        if re.search(r"^\s*set type (aggregate|redundant)\b", config, re.M):
            found["interface aggregate"] = ["aggregate", "LACP", "802.3ad"]
        if re.search(r"^\s*set vlanid \d+", config, re.M):
            found["interface vlan"] = ["VLAN"]
    if "config system sdwan" in found and not re.search(r"config system sdwan\s*\n\s*set status enable", config):
        found.pop("config system sdwan")
    return found


# --------------------------------------------------------- release notes IO
def extract_text(data: bytes, filename: str = "") -> str:
    name = filename.lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    text = data.decode("utf-8", errors="replace")
    if "<html" in text[:2000].lower() or name.endswith((".htm", ".html")):
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text


def fetch_url(url: str) -> str:
    r = httpx.get(url, follow_redirects=True, timeout=60, headers={"User-Agent": "FortiUpgrade/1.0"})
    r.raise_for_status()
    return extract_text(r.content, url)


def _paragraphs(text: str):
    """Yield (section, paragraph) pairs. Bug-table rows ("1234567 Description...") are
    kept as individual paragraphs."""
    section = "introduction"
    buf: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        if 0 < len(s) < 60 and any(low.startswith(h) for h in SECTION_HEADINGS):
            if buf:
                yield section, " ".join(buf)
                buf = []
            section = low
            continue
        if re.match(r"^\d{6,7}\b", s) or not s:
            if buf:
                yield section, " ".join(buf)
            buf = [s] if s else []
            continue
        buf.append(s)
    if buf:
        yield section, " ".join(buf)


def scan_release_notes(text: str, config: str, label: str = "") -> list[dict]:
    feats = feature_inventory(config)
    hits = []
    seen = set()
    for section, para in _paragraphs(text):
        if not any(k in section for k in INTERESTING):
            continue
        for prefix, terms in feats.items():
            for t in terms:
                if re.search(r"(?<![\w-])" + re.escape(t.strip()) + r"(?![\w-])", para,
                             0 if t.isupper() else re.I):
                    k = (prefix, para[:120])
                    if k in seen:
                        break
                    seen.add(k)
                    bug = re.match(r"^(\d{6,7})\b", para)
                    hits.append({"source": label, "section": section, "feature": prefix, "term": t.strip(),
                                 "bug_id": bug.group(1) if bug else None,
                                 "text": para[:600] + ("..." if len(para) > 600 else ""),
                                 "severity": "warning" if any(x in section for x in
                                                              ("known", "special", "removed", "deprecated",
                                                               "default behavior")) else "info"})
                    break
    return hits


def analyse(config: str, current: str, target: str, model: str = "",
            notes: list[tuple[str, str]] | None = None, extra_rules: Path | None = None) -> dict:
    return {
        "features": sorted(feature_inventory(config).keys()),
        "rule_hits": evaluate_rules(config, current, target, model, extra_rules),
        "notes_hits": [h for label, text in (notes or []) for h in scan_release_notes(text, config, label)],
        "notes_sources": [label for label, _ in (notes or [])],
    }


# ------------------------------------------------- Fortinet document library
DOCS = "https://docs.fortinet.com"
UA = {"User-Agent": "FortiGateUpgrader/0.3"}


def notes_url(version: str) -> str:
    return f"{DOCS}/document/fortigate/{str(FosVersion.parse(version))}/fortios-release-notes/"


def _article(html_text: str) -> str:
    """The article part of a docs.fortinet.com page (drops the site navigation)."""
    m = re.search(r"(?is)<h1\b.*", html_text)
    body = m.group(0) if m else html_text
    body = re.split(r"(?i)<footer\b|class=\"[^\"]*(?:pagination|feedback)", body)[0]
    return extract_text(("<html>" + body).encode(), "x.html")


def _pages(version: str, client: httpx.Client) -> tuple[str, list[str]]:
    v = str(FosVersion.parse(version))
    r = client.get(notes_url(v))
    r.raise_for_status()
    links = sorted(set(re.findall(rf'href="(/document/fortigate/{re.escape(v)}/fortios-release-notes/\d+/[^"#?]+)"',
                                  r.text)))
    return r.text, links


def fetch_fortinet_notes(version: str) -> dict:
    """Download every page of the FortiOS release notes for `version`.
    Returns {"text": full text, "known_issues": [...], "url": ...}."""
    from concurrent.futures import ThreadPoolExecutor
    with httpx.Client(follow_redirects=True, timeout=60, headers=UA) as client:
        first, links = _pages(version, client)
        if not links:
            raise ValueError(f"No release notes found for FortiOS {version}")

        def get(link):
            try:
                return link, client.get(DOCS + link).text
            except httpx.HTTPError:
                return link, ""
        with ThreadPoolExecutor(6) as ex:
            pages = dict(ex.map(get, links))
    text = "\n\n".join(_article(pages[l]) for l in links if pages.get(l))
    ki = next((l for l in links if l.endswith("/known-issues")), None)
    known = parse_known_issues(pages.get(ki, "")) if ki else []
    return {"text": text, "known_issues": known, "url": DOCS + ki if ki else notes_url(version)}


def parse_known_issues(html_text: str) -> list[dict]:
    """Rows of the Known issues tables: [{bug_id, category, group, description}]."""
    out = []
    group, category = "Known issues", ""
    for m in re.finditer(r"(?is)<h2\b[^>]*>(.*?)</h2>|<h3\b[^>]*>(.*?)</h3>|<tr\b[^>]*>(.*?)</tr>", html_text):
        h2, h3, tr = m.groups()
        clean = lambda s: " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())  # noqa: E731
        if h2:
            group = clean(h2)
            category = ""
        elif h3:
            category = clean(h3)
        else:
            cells = [clean(c) for c in re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", tr)]
            if len(cells) >= 2 and re.fullmatch(r"\d{5,8}", cells[0]):
                out.append({"bug_id": cells[0], "category": category or group, "group": group,
                            "description": cells[1]})
    return out


# category heading on the Known issues page -> config prefixes that make it relevant
CATEGORY_FEATURES = {
    "ipsec": ["config vpn ipsec"], "vpn": ["config vpn ipsec", "config vpn ssl"],
    "ssl vpn": ["config vpn ssl"], "agentless": ["config vpn ssl"],
    "routing": ["config router bgp", "config router ospf", "config router policy", "config router multicast"],
    "sd-wan": ["config system sdwan", "config system virtual-wan-link"],
    "high availability": ["config system ha"], "ha": ["config system ha"],
    "proxy": ["config web-proxy", "config firewall proxy-policy"], "explicit proxy": ["config web-proxy"],
    "wifi": ["config wireless-controller"], "fortilink": ["config switch-controller"],
    "fortiswitch": ["config switch-controller"], "switch controller": ["config switch-controller"],
    "ztna": ["config firewall access-proxy", "config system ztna"], "user": ["config user"],
    "authentication": ["config user"], "log": ["config log"], "security fabric": ["config system csf"],
    "hyperscale": ["config system npu"], "ips": ["config ips"], "web filter": ["config webfilter"],
    "dns filter": ["config dnsfilter"], "antivirus": ["config antivirus"], "explicit": ["config web-proxy"],
}
ALWAYS = ("upgrade", "system", "firewall", "gui", "rest api", "fortiview", "policy")


def rate_known_issues(issues: list[dict], config: str, model: str = "") -> list[dict]:
    """Mark each known issue as relevant when it touches a feature this config uses."""
    feats = feature_inventory(config) if config else {}
    model_l = (model or "").lower()
    out = []
    for k in issues:
        cat = k["category"].lower()
        why = []
        text = cat + " " + k["description"].lower()
        if re.search(r"6000|7000|\b6k\b|\b7k\b|hyperscale|slbc|\bfim\b|\bfpm\b", text) \
                and not re.search(r"-?[67]\d{3}[a-z]", model_l):
            out.append({**k, "relevant": False, "reason": "FortiGate 6000/7000 or hyperscale only"})
            continue
        named = re.findall(r"\b(?:FG|FGT|FWF|FortiGate|FortiWiFi)[- ]?(\d{2,4}[A-Z]{1,2})\b", k["description"])
        if named and not any(n.lower() in model_l for n in named):
            out.append({**k, "relevant": False, "reason": f"Specific to {', '.join(sorted(set(named)))}"})
            continue
        if "vm" in model_l and re.search(r"\bnp[67](xlite|lite)?\b|\bsoc\d?\b|asic", text):
            out.append({**k, "relevant": False, "reason": "Hardware acceleration (not on VMs)"})
            continue
        if cat in ("vm", "fortigate-vm") and "vm" not in model_l:
            out.append({**k, "relevant": False, "reason": "FortiGate-VM only"})
            continue
        for key, prefixes in CATEGORY_FEATURES.items():
            if re.search(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])", cat) and \
                    any(f.startswith(p) for f in feats for p in prefixes):
                why.append(f"config uses {k['category']}")
                break
        for prefix, terms in feats.items():
            if any(re.search(r"(?<![\w-])" + re.escape(t.strip()) + r"(?![\w-])", k["description"],
                             0 if t.isupper() else re.I) for t in terms):
                why.append(f"mentions {terms[0].strip()}")
                break
        if cat in ("vm", "fortigate-vm", "virtual machine") and "vm" in model_l:
            why.append("this is a FortiGate-VM")
        if not why and any(cat.startswith(a) for a in ALWAYS):
            why.append("general")
        out.append({**k, "relevant": bool(why) and why != ["general"], "reason": ", ".join(why) or ""})
    return out
