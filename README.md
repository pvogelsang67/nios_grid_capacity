# NIOS Grid Capacity Collector

`nios_grid_capacity.py` collects capacity information for **every member** of an
Infoblox NIOS Grid via the WAPI (REST) API and writes it to a single CSV file,
one row per member. It uses two WAPI calls:

1. **List members** — `GET /wapi/<ver>/member?_return_fields=host_name,dns_resolver_setting,syslog_servers,additional_ip_list,mgmt_port_setting,node_info`
2. **Per-member capacity** — `GET /wapi/<ver>/capacityreport?name=<host>&_return_fields=object_counts,total_objects,hardware_type,max_capacity,name,role,percent_used`

For each member discovered in step 1, the script runs the capacity report in
step 2 and merges the results into one flat CSV row.

## Author & License
Author: Pat Vogelsang
Licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## Requirements
- Python 3.8 or newer
- **Standard library only.** NIOS WAPI is plain HTTPS with HTTP Basic
  authentication, which Python's built-in `urllib` handles natively, so no
  third-party HTTP client (such as `requests`) is required. There is nothing to
  `pip install`. The `.env` file is parsed by a tiny built-in parser, so
  `python-dotenv` is **not** required either.

### Recommended VS Code extensions
- Microsoft **Python** and **Pylance** — the verbose docstrings and type hints
  surface well in IntelliSense/hover tooltips.

## Inputs
Every input can be supplied **on the command line** or in a **`.env` file**.
Resolution order (highest priority first):

1. Command-line argument.
2. Matching key in the `.env` file.
3. **For the password only:** a secure, no-echo interactive prompt.

| Command line | `.env` key | Required | Description |
|--------------|-----------|----------|-------------|
| `-g, --grid-manager` | `NIOS_GRID_MANAGER` | yes | IP or FQDN of the Grid Manager (bare host or `https://host` both accepted). |
| `-u, --username` | `NIOS_USERNAME` | yes | WAPI (NIOS admin) username. |
| `-o, --output` | `NIOS_OUTPUT` | yes | Path where the CSV is written (directory must exist and be writable). |
| `-p, --password` | `NIOS_PASSWORD` | no* | WAPI password. *Not required anywhere — if absent, you are prompted securely. |
| `--wapi-version` | `NIOS_WAPI_VERSION` | no | WAPI version in the URL (default `v2.14`). |
| `--page-size` | `NIOS_PAGE_SIZE` | no | Members per page when enumerating (default `1000`). |
| `--timeout` | `NIOS_TIMEOUT` | no | Per-request timeout in seconds (default `60`). |
| `--ca-cert` | `NIOS_CA_CERT` | no | PEM CA bundle to verify the Grid Manager's TLS certificate. |
| `--insecure` | `NIOS_INSECURE` | no | Disable TLS verification (`true`/`false` in `.env`). |
| `--env-file` | — | no | Path to the `.env` file (default: `.env` in the current directory). |
| `-v, --verbose` | — | no | Enable DEBUG logging. |

### The `.env` file
Copy the provided template and edit it:

    cp .env.example .env

Format rules: one `KEY=value` per line; blank lines and `#` comment lines are
ignored; an optional leading `export ` is allowed; values may be quoted with
`'` or `"` (quote the password if it contains a `#`); for unquoted values an
inline ` #` starts a comment. A missing default `./.env` is fine (just supply
everything on the command line); a missing **custom** `--env-file` path is an
error.

### Providing the password (in priority order)
1. `--password` on the command line (**discouraged** — visible in shell history / process list).
2. `NIOS_PASSWORD` in the `.env` file.
3. `NIOS_PASSWORD` as a real environment variable (handy for CI).
4. **Secure prompt** — if none of the above is set, the script asks for it and
   the input is hidden/obscured (never echoed to the screen).

Never commit credentials. The included `.gitignore` excludes the real `.env`
(while keeping `.env.example`) and generated `*.csv` files.

### TLS / certificates
Verification is **on by default**. NIOS Grid Managers frequently present a
self-signed certificate; for those use either `--insecure` /
`NIOS_INSECURE=true` (labs, **not** production), or `--ca-cert` /
`NIOS_CA_CERT` pointing at your own/corporate CA bundle. Setting both a CA
certificate and insecure mode is rejected.

## How to Run
Clone the repository and show the help text:

    git clone <repo-url>
    cd <repo>
    ./nios_grid_capacity.py -h

All inputs on the command line, prompt for the password (recommended):

    ./nios_grid_capacity.py --grid-manager 192.168.2.20 \
        --username admin --output grid_capacity.csv

All inputs (including the password) in a `.env` file in the current directory:

    cp .env.example .env      # then edit .env
    ./nios_grid_capacity.py   # reads ./.env automatically

Use a named `.env` but override the output path on the command line
(command line wins):

    ./nios_grid_capacity.py --env-file prod.env --output prod_capacity.csv

Lab grid with a self-signed certificate:

    ./nios_grid_capacity.py -g 192.168.2.20 -u admin -o out.csv --insecure

(On Windows, run with `py nios_grid_capacity.py ...`.)

## Outputs
A CSV file at `--output` with **one row per Grid member**. Columns, in order:

1. **Member info** — `host_name`, `member_ref`, management-port settings, DNS
   resolvers/search domains, syslog server count/addresses, additional IP count,
   and per-node hardware details (`node1_*`, `node2_*` for HA pairs):
   `ha_status`, `host_platform`, `hwid`, `hwmodel`, `hwtype`, `hypervisor`,
   `paid_nios`, and a summarized `service_status`.
2. **Capacity summary** — `cap_report_found`, `cap_name`, `cap_role`,
   `cap_hardware_type`, `cap_max_capacity`, `cap_total_objects`,
   `cap_percent_used`.
3. **Per-object-type counts** — one `obj_<type_name>` column for every object
   type reported by *any* member (the union across all members, sorted). If a
   member does not report a given type, that cell is blank.

If a member has no capacity report, its row is still written with the capacity
columns left blank and `cap_report_found` set to `False`.

## Troubleshooting
- **HTTP 401 / authentication failed** — check `--username` and the password.
- **Connection errors / TLS failures** — verify the `--grid-manager` address and
  network reachability. For self-signed certificates, add `--insecure` or supply
  `--ca-cert`.
- **A member is missing capacity numbers** — the script logs a warning and
  continues; the member still appears as a row with `cap_report_found = False`.
- **Wrong WAPI version** — set `--wapi-version` to match your NIOS release.
- Re-run with `-v/--verbose` to see each request URL and per-member progress.
