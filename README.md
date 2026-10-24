# Teltonika NTP Server for Home Assistant

[![Hassfest](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/hassfest.yml/badge.svg?branch=main)](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/hassfest.yml)
[![HACS validation](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/hacs.yml/badge.svg?branch=main)](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/hacs.yml)
[![CodeQL](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/codeql.yml)
[![Ruff](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/ruff.yml/badge.svg?branch=main)](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/ruff.yml)
[![Tests](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/HuisAutomatisering/teltonika-ntp-ha/actions/workflows/tests.yml)

[![HACS custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Quality scale: Bronze (aligned)](https://img.shields.io/badge/quality%20scale-bronze%20(aligned)-cd7f32.svg)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Release](https://img.shields.io/github/v/release/HuisAutomatisering/teltonika-ntp-ha?display_name=tag)](https://github.com/HuisAutomatisering/teltonika-ntp-ha/releases)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=HuisAutomatisering&repository=teltonika-ntp-ha&category=integration)

Monitor a Teltonika GNSS-disciplined NTP server, such as the **NTP001**, from Home
Assistant: is it serving time, is it synchronized, and how far does its clock sit from
your Home Assistant host.

## Why not the built-in Teltonika integration?

Home Assistant core ships a [Teltonika](https://www.home-assistant.io/integrations/teltonika/)
integration for RutOS routers. It only monitors cellular modems, so on a device without a
modem, such as the NTP001, setup fails with `Service does not exist in device` and keeps
retrying (and logging in) every few seconds. This integration is written for the NTP
servers instead and leaves routers to the core integration.

## Entities

| Entity | Type | Description |
| --- | --- | --- |
| Synchronization | Sensor (enum) | `synchronized`, `unsynchronized`, `rate_limited` or `no_response` |
| Stratum | Sensor | 1 when the server is locked to GNSS |
| Reference | Sensor | Time source, for example `GPS` |
| Last GPS sync | Sensor (timestamp) | When GPS last set the server clock (normally every ~16 s) |
| Maximum error | Sensor (ms) | Worst-case error of the server clock; grows while GPS is lost |
| Offset | Sensor (ms) | Server clock minus the Home Assistant host clock |
| Round-trip delay | Sensor (ms, diagnostic) | Network round trip of the NTP query |
| Root dispersion | Sensor (ms, diagnostic) | Error estimate the server reports for its own time |
| NTP service | Binary sensor (connectivity) | The server answers NTP requests |
| Time sync | Binary sensor (problem) | On when the server is not confirmed synchronized |

The device shows up with its model, serial number, hardware revision and firmware version,
read from the RutOS API.

## How it works

- **NTP status** comes straight from the NTP service: once per minute the integration sends
  a single SNTP request (UDP 123) and reads leap indicator, stratum, reference and timing
  from the reply. That is the same view every other client on your network gets.
- **Device information** comes from the RutOS REST API (`/api`) after logging in with the
  WebUI credentials. The session token is reused until it expires, so the device log does
  not fill up with logins.

### About rate limiting

ntpd on the NTP001 rate limits clients that ask too often and then answers with a
"kiss-o'-death" `RATE` packet. One query per minute is far below that limit, but if the
Home Assistant host also uses this server as its own time source, and something else on
the same IP address sends bursts (`sntp`, `ntpdate`), the server may answer `RATE` for a
while. The Synchronization sensor then shows **Rate limited**, which says nothing bad about
the server itself.

## Holdover monitoring

When the NTP001 loses GPS (a disconnected antenna, snow on it, a blocked view of the sky), it
keeps serving time from its own crystal. That is called holdover. Two sensors show it:

- **Last GPS sync** stops moving. It is the reference timestamp from the NTP reply: the
  moment GPS last set the server clock. While GPS is fine it updates every ~16 seconds.
- **Maximum error** starts climbing. It is the NTP root distance (RFC 5905): half the root
  delay plus the root dispersion, plus 15 µs for every second since the last GPS update,
  the drift NTP allows an undisciplined clock. One hour without GPS adds 54 ms.

Maximum error is an upper bound, not a measurement: the real drift is usually much smaller,
but without GPS there is nothing to measure it against. Weather barely matters for GPS
timing, so do not expect these values to follow the forecast.

The **Offset** sensor compares the server with the Home Assistant host, so it mostly shows
how well the Home Assistant host keeps time and how symmetric the network path is.

### Example: notify when GPS is lost

```yaml
alias: NTP server lost GPS
triggers:
  - trigger: template
    value_template: >-
      {{ states('sensor.ntp001_last_gps_sync') not in ['unknown', 'unavailable']
         and now() - as_datetime(states('sensor.ntp001_last_gps_sync'))
             > timedelta(minutes=5) }}
actions:
  - action: notify.notify
    data:
      title: NTP server in holdover
      message: >-
        No GPS update since
        {{ as_local(as_datetime(states('sensor.ntp001_last_gps_sync'))).strftime('%H:%M') }}.
        Maximum error is now {{ states('sensor.ntp001_maximum_error') }} ms.
```

Adjust the entity IDs if your device has another name.

## Limitations

- GNSS details (fix, satellites, position) are not available: the RutOS API on the NTP001
  does not expose a GPS or GNSS service. Synchronization, stratum and reference already
  show whether the server is locked to GNSS.

## Installation

### HACS (custom repository)

1. Open HACS, go to **Integrations**, open the menu (three dots) and choose **Custom repositories**.
2. Add `https://github.com/HuisAutomatisering/teltonika-ntp-ha` with category **Integration**.
3. Install **Teltonika NTP Server** and restart Home Assistant.

Or use the "Open in HACS" button above.

### Manual

Copy `custom_components/teltonika_ntp` to the `custom_components` folder of your Home
Assistant configuration and restart.

## Configuration

The device is discovered through DHCP. You can also add it by hand: **Settings → Devices &
services → Add integration → Teltonika NTP Server**.

| Field | Description |
| --- | --- |
| Host | IP address or host name. HTTPS is tried first, then HTTP. |
| Username / Password | WebUI credentials. A dedicated read-only user is recommended. |
| Verify SSL certificate | Leave off for the default self-signed certificate. |

Make sure **Enable NTP server** is on in the device's **Time** settings.

If the core Teltonika integration also discovered the device, ignore that discovery, or
remove its entry so it stops retrying.

## Removal

1. Go to **Settings → Devices & services** and open **Teltonika NTP Server**.
2. Open the menu (three dots) next to the device entry and choose **Delete**.
3. To remove the files as well: in HACS, open **Teltonika NTP Server** and choose
   **Remove**, or delete `custom_components/teltonika_ntp` for a manual installation.
   Restart Home Assistant afterwards.

Nothing is changed on the device itself. If you created a dedicated WebUI user for Home
Assistant, you can delete it in the device's WebUI.

## Quality scale

This integration is aligned with the **Bronze** tier of the
[Home Assistant integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/).
Home Assistant only grades integrations that ship with Home Assistant itself, so this is a
self-assessment, not an official rating. The status per rule is recorded in
[`quality_scale.yaml`](custom_components/teltonika_ntp/quality_scale.yaml).

## Development

The tests use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
and run on every push. To run them locally (Python 3.14):

```bash
pip install -r requirements_test.txt
pytest --cov --cov-report=term-missing
```

## Credits

The RutOS login and session handling follow the approach of
[teltasync](https://codeberg.org/dmho/teltasync) by Karl Beecken (Apache License 2.0), the
library behind the core Teltonika integration. It is reimplemented here with plain aiohttp,
so this integration has no extra requirements.

The URL handling (HTTPS first, HTTP fallback) and the structure of the config flow are based
on the [Home Assistant core Teltonika integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/teltonika)
(Apache License 2.0).

The Teltonika icons come from the
[home-assistant/brands](https://github.com/home-assistant/brands) repository. Teltonika is a
trademark of its owner; this project is not affiliated with Teltonika.

## License

[MIT](LICENSE)
