# FortiGate Upgrader

A local desktop app for macOS and Windows that upgrades FortiGate firmware through the FortiOS REST API. It can upgrade one device or many at once, and covers standalone units, FGCP clusters and FGSP peers in VDOM and non-VDOM mode. Around each upgrade it runs pre/post checks with a diff, validates the upgrade path, checks the configuration against release notes and detects FortiManager management.

FortiGate Upgrader is an independent tool and is not affiliated with Fortinet.

## Download

Get the latest build from **[Releases](https://github.com/fsecuritynz/FortiGate-Upgrader/releases/latest)**:

| Platform | File |
|---|---|
| macOS, Apple silicon | `FortiGate-Upgrader-<version>-macOS-arm64.dmg`: open it and drag the app to Applications |
| macOS, Intel | `FortiGate-Upgrader-<version>-macOS-x86_64.dmg` (when present) |
| Windows 10/11 x64 | `FortiGate-Upgrader-<version>-Windows-x64-Setup.exe`: installs for the current user, no admin rights needed |

`SHA256SUMS.txt` in each release lists the files' checksums. The apps aren't code-signed:
- **macOS:** on first launch, right-click the app and choose Open.
- **Windows:** SmartScreen may ask you to confirm (More info › Run anyway).

## Updates

The app checks this repository's latest release at start-up and every 6 hours. Only the release list and the download are fetched; nothing about you or your devices is sent. When a newer version is out:
- **Notice:** a badge appears under the version at the bottom left, and a prompt shows the release notes.
- **Download and install:** fetches the file for your platform and verifies it against the release's `SHA256SUMS.txt`, refusing a mismatch. The app then closes, updates itself and reopens.
- **How it installs:** on macOS the app bundle is replaced in place from the disk image; on Windows the installer runs silently.
- **Settings › Software updates:** shows the installed and latest versions, has **Check now**, and turns automatic checks off.

### Publishing a release

1. Bump `__version__` and `__build__` in `fortiupgrade/__init__.py`, and add the entry at the top of `fortiupgrade/changelog.json`.
2. Commit, then tag and push: `git tag v0.11.0 && git push origin main v0.11.0`.
3. The **Release** workflow (`.github/workflows/release.yml`) does the rest:
   - checks the tag matches the version and the change log
   - runs the tests
   - builds the macOS `.dmg` (`packaging/build_mac.sh` + `packaging/make_dmg.sh`) and the Windows installer (`packaging/build_windows.ps1` with Inno Setup, `packaging/installer.iss`)
   - publishes the Release with checksums and notes taken from the change log

## Run from source

```bash
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m fortiupgrade             # native window
python -m fortiupgrade --browser   # or in your default browser
```

The UI talks to a local server bound to `127.0.0.1` that requires a per-launch token. API tokens and SSH passwords go to the OS keychain under "FortiGate Upgrader". Data lives in `~/Library/Application Support/FortiGate Upgrader` on macOS and `%APPDATA%\FortiGate Upgrader` on Windows. Data and keychain entries from earlier "FortiUpgrade" builds are migrated automatically.

## Build

PyInstaller does not cross-compile, so build on each OS:

- macOS: `packaging/build_mac.sh` → `dist/FortiGate Upgrader.app` (unsigned)
- Windows: `packaging\build_windows.ps1` → `dist\FortiGate Upgrader\FortiGate Upgrader.exe` (needs the WebView2 runtime, included with Windows 11)
- App icon: `packaging/icon.icns` (macOS) and `packaging/icon.ico` (Windows), both drawn by `packaging/make_icon.py`. Run `.venv/bin/python packaging/make_icon.py` to regenerate them after changing the artwork.

## Versions

The version and build (FortiOS style, e.g. `v0.10.0 build0017`) are in `fortiupgrade/__init__.py`. They appear at the bottom left of the window, in the macOS bundle and in PDF reports. **System › Change log** lists every release from `fortiupgrade/changelog.json`. Every update bumps the version and build and adds an entry at the top of the change log; a test checks they agree.

## Using it

1. **Devices**: add devices one at a time with **Create New**, or many at once with **Import CSV** (`name,host,port,api_token[,type][,ssh_user,ssh_password]`). The inventory shows model, firmware, HA, VDOMs and whether each device is managed by FortiManager.
2. **Device dashboard**: click a device name to open it. It has four tabs:
   - **Dashboard** (live): hostname and IP, model, serial number, firmware and its maturity, HA or standalone, VDOMs, FortiManager, licences with expiry dates, CPU/memory/disk, HA members, notices (expiring licences, HTTP/TELNET admin access, newer firmware) and a front-panel drawing of the unit built from its physical interfaces.
   - **Current status**: the latest data collection, or **Collect now** for a fresh one. It shows interfaces with admin access, SD-WAN zones, ARP, BGP/OSPF neighbours, routing table, IPsec tunnels, event logs and the known issues of the running release. Neighbours, routes, IPsec and logs each have a **Compare** button to diff against any earlier collection.
   - **Configuration**: every stored version of the device's configuration. That covers every data collection and every **Fetch current config**, which is read-only and stored as `<hostname>-<Weekday>-<date>.conf` unless it is identical to the newest stored version.
     - **View**: FortiOS syntax highlighting, a section navigator, search, secrets masked (with a toggle), copy and download.
     - **Compare**: any two stored versions, or any version against **Current** (read live). Shown side by side, unified, or as a list of changed settings with before/after values.
   - **Upgrades**: every upgrade and simulation run on the device, with path, stages, pre/post diff and full log.
   - **Jobs**: anything running on the device now, plus recent activity.
   Known issues come from the FortiOS release notes on docs.fortinet.com, downloaded once per version and cached under `release_notes/`. Each issue is rated against the features the device's configuration uses.
3. **Firmware Store**: upload `.out` images, or import them from disk. Version, build and Hardware tags are read from Fortinet file names (`FGT_60F-v7.4.8.F-build2795-FORTINET.out`) and can be edited, along with free-form tags.
4. **Upgrade**: a step-by-step installer. The bar at the top always shows the **target image** (version, build, maturity) and the **install method**.
   1. *Devices*: tick one or more devices.
   2. *Target image*: pick from the versions FortiGuard offers for those devices (newest first; the latest Mature release is suggested), or type one. Choose Fortinet's supported upgrade path or a direct install.
   3. *Install method*: **Download from FortiGuard** (each FortiGate fetches the image itself) or **Push a local image** (this computer uploads the matching `.out` file from the Firmware Store; you can add one here).
   4. *Options*: HA mode for FGCP clusters and FGSP peers, before/after checks, and whether to pause for approval after the pre-checks.
      - **Default probes:** the wizard adds a probe set for every physical unit being upgraded, marked *Auto*, so the timeline shows each unit's availability separately. For an HA cluster that means each unit's own `management-ip`, or its dedicated HA management interface. The shared cluster address is never used, because it follows whichever unit is primary. **Ping** is added only where the interface allows ping, and **HTTPS** only where it allows HTTPS, on the admin HTTPS port (`admin-sport`). Remove any you don't want; your own probes are remembered.
      - **Finding each unit's address:** a unit's own address comes from the addresses saved on the device. Otherwise, if the device has SSH credentials, the app reads it from the primary: `execute ha manage <n>` followed by `show system interface`, which is read-only. Units it can't find are listed so you can add them.
      - **Probe rate limits:** HTTP(S) probes run at most once a second (default every 2 s), because each one is a full new TLS session to the target. Probing a FortiGate's admin page at 100 ms loads its management web server enough that the upgrade's own API calls can be refused.
      - **Service availability probes** (optional) measure the real outage window. This computer runs rapid **Ping** (default every 200 ms), **TCP connect** (host and port) or **HTTP(S)** (URL, optional expected status such as `200` or `2xx`) probes. They run from the pre-checks until a settle period after the last device finishes, and longer if a service is still down. Pick targets whose traffic crosses the FortiGate (a server behind it, a VIP, an internet address); **+ Selected FortiGates** adds the management/cluster address. **Test now** checks each target answers before you start. Probes work the same for standalone units, FGCP and FGSP.
      - Failures lasting at least the outage threshold (default 1 s) are **outages**; shorter ones are packet loss. Each outage is tagged with the upgrade step that preceded it (image install and reboot, HA failover, HA sync), so you can see which step caused it.
      - The Install page shows a live timeline per probe, with up/down status, availability, outage count, longest and total downtime, and latency before vs after. The same view is kept in the device's **Upgrades** history and in the PDF report.
      - Ping uses an unprivileged ICMP socket on macOS (no admin rights) and the system `ping` command on Windows.
   5. *Pre-flight & review*: a read-only pre-flight runs with its log streaming live, then a summary per device (path, image, HA mode, findings). Critical findings and FortiManager-managed devices each need a tick before **Start upgrade** is enabled. **Simulate** runs every step without changing anything.
   6. *Install*: overall progress, one card per device (Pre-checks → Transfer → Install & reboot → Post-checks → Done) and a terminal-style log of everything happening. It covers each check and its result, every REST API and SSH call (method, path, status, time, size), image read/encode/upload with throughput, FortiGuard download responses, and the reboot countdown and status polls. You can filter by device, hide API calls, pause auto-scroll, copy or download the log. Results with diff links appear at the end. A **PDF report** of the whole run is generated automatically, with **Open PDF**, **Show in folder** and **Download**. It covers:
      - the settings: target image, install method, path and HA modes
      - pre-flight findings and the release-note analysis
      - per device: the image used (FortiGuard build ID, or file name and SHA-256), a timeline, warnings and errors, and a before/after table for every check
      - every check difference with its severity, and the configuration differences
      - service availability: the probe timeline, per-probe statistics and every outage window
      - the full activity log

      Reports are saved as `reports/upgrades/upgrade-report-<device>-<Weekday>-<date>.pdf`. Past upgrades in a device's **Upgrades** tab have a **PDF report** button too.
5. **Snapshots & Diff**:
   - Every data collection is saved as `<hostname>-<Weekday>-<dd-mm-yyyy-hh-mm-ss>.log`, for example `BR-01-Thursday-05-11-2026-14-03-22.log`.
   - Each file is a human-readable report: summary, one table per check and VDOM, CLI output and the config backup. A JSON copy beside it is used for diffs.
   - Comparisons are graded Critical / Warning / Info and show readable before → after values.

## FortiGate prerequisites

- A **REST API admin** with read/write on System (firmware, HA, config) and read on everything else. Add this computer to its trusted hosts.
- Optional **SSH credentials**. These enable the TAC report, BGP advertised routes, `diagnose sys ha checksum cluster` and the crashlog.
- FortiGuard connectivity and a valid contract, for FortiGuard downloads and the device's own upgrade-path data.

## Checks

| Scope | Checks |
|---|---|
| Per VDOM | Interfaces (status, IP, type, role, admin access, speed, duplex), SD-WAN zones and members, ARP, IPv4/IPv6 routes, BGP neighbours with received prefixes and best paths, BGP advertised prefixes (SSH), OSPF neighbours, IPsec tunnels, SSL VPN users, SD-WAN health checks, firewall policy counts, event logs (system, VPN, routing, HA, SD-WAN, user), DHCP leases |
| Global | System status, HA cluster sync, FortiGuard licences, CPU / memory / sessions, CLI captures, TAC report |
| Config | Structured and unified diff, with ENC secrets and certificates masked |

## HA upgrade modes

- **FGCP**: `simultaneous`, `uninterruptible`, `local-only` or `secondary-only`.
  - On 7.4.1+ the app sets `upgrade-mode`. Earlier builds only have `uninterruptible-upgrade`, which supports the first two.
  - The original setting is restored afterwards.
- **FGCP service-gated** (app-controlled, FortiOS 7.4.1+): the app sets `upgrade-mode local-only` and upgrades one unit at a time through each unit's own address, so it decides when the cutover happens:
  1. Upgrade each secondary, wait for it to come back on the new firmware and rejoin the cluster.
  2. Hold until every service probe has had no failures for the chosen time (default 60 s). Without probes, wait that long for the cluster to settle. If services don't settle within the limit (default 15 min), ask before carrying on.
  3. Optionally ask for your go-ahead.
  4. Upgrade the primary. Its reboot is the cutover to the upgraded unit.
  5. Wait for it to rejoin, the cluster to sync, and the services to be stable again.
  - **Avoid a fail-back** (on by default): with override disabled, FGCP picks the unit with the longer HA uptime, but ignores differences under `ha-uptime-diff-margin` (300 s by default) and then compares priority. If the old primary has the higher priority and rejoins within the margin, it takes the primary role back, causing a second failover. The app times how long the secondary took to come back and holds the cutover until the upgraded unit will be older than the margin when the old primary returns. When the upgraded unit has the higher priority there's nothing to wait for. With override on, priority always wins, so the app says so instead of waiting. Afterwards it reports which unit ended up primary.
  - Needs the cluster address plus every unit's own address (Add device detects them). With FortiGuard download each secondary fetches its own image, so it needs its own route to FortiGuard; otherwise push a local image.
- **FGSP**:
  - `uninterruptible`: rolling, one peer at a time, with post-checks and an approval step between peers.
  - `simultaneous`: all peers at once.
  - `local-only`: only the peers you select.
- **Choosing the mode:** the wizard's Options step lists every HA device in the upgrade with the HA mode it reports, and asks how each FGCP cluster should upgrade and fail over (and how FGSP peers should roll).
- **Detected HA:** a unit saved as *Standalone* that reports FGCP or FGSP is upgraded as a cluster with the mode you choose. Otherwise FortiOS would use whatever upgrade mode happens to be configured on it. The wizard, the device list and the dashboard point this out, and offer to save the device with the right type.

### HA cluster view

A device in an FGCP cluster shows every unit on its dashboard:
- the appliance drawing for each unit
- the HA heartbeat cable between their heartbeat interfaces
- each unit's role (primary/secondary), HA priority, serial and own address
- the cluster's mode, group, override, monitored interfaces and shared address

If the device is saved with one unit's own address, or a unit has no address yet, **Save addresses** re-saves it as an HA cluster: the cluster address first, then each unit's own address. The existing API token and SSH login are reused, because HA keeps admin accounts and API users the same on every unit.

### Which management address to use for an FGCP cluster

- **First address: the cluster's shared management IP (required).** This is an interface address that only the current primary answers on, and it moves with the primary on failover. Every upgrade goes through it: the primary takes the image, sends it to the secondaries and runs the upgrade in the chosen HA mode.
- **Extra addresses: each unit's dedicated HA management interface (optional).** Set with `config system ha` › `set ha-mgmt-status enable` › `config ha-mgmt-interfaces`. These reach one specific unit, whichever unit is primary. Add them to use *Local only* and *Secondary only*, to confirm each unit's version after its reboot, and to run checks on secondaries. Don't enter a secondary's ordinary interface IP: secondaries don't answer on the cluster's shared addresses.
- **FGSP:** every peer is a separate FortiGate; add each peer's own management address.
- **Service probes:** the cluster address shows the outage users see during failover; a unit's own address shows when that unit reboots. **+ Selected FortiGates** adds both.

## FortiManager

The app detects `config system central-management` with `type fortimanager`. Before you can upgrade such a device it shows these warnings and requires an explicit acknowledgement:

- Check that the FortiManager and ADOM support the target version.
- Install or discard pending changes in FortiManager.
- Disable FortiManager firmware templates for the device.
- Run Retrieve Config in FortiManager afterwards.
- Preferably upgrade from FortiManager instead.

## Testing without hardware

```bash
python tools/mock_fortigate.py --port 18443 --ha a-p --vdom --fmg 10.10.0.5 --break   # token: mocktoken, http
pytest -q tests
```

The mock is a small simulator of the REST endpoints the app uses, not a FortiOS emulator.

## Verify before production use

- `POST monitor/system/firmware/upgrade`: for `source: fortiguard` the `filename` is the image `id` from `monitor/system/firmware`. For `source: upload` the image is sent base64-encoded in `file_content`.
- The response format of `monitor/system/firmware/upgrade-paths` is not publicly documented. The parser accepts `{from:{major,minor,patch}, to:{...}}` entries.
- Config backup is a GET on 7.4 and earlier and a POST on 7.6+. The app tries both.
- The built-in release-note rules (`fortiupgrade/data/rules.yaml`) cover a few well-known changes only.

Test against a lab unit before using the app on production devices.
