#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# nios_grid_capacity.py
#
# Collect capacity information for every member of an Infoblox NIOS Grid via the
# WAPI (REST) API and write the results to a single CSV file (one row per
# member). The script performs two kinds of WAPI calls:
#
#   1. GET /wapi/<ver>/member          -> discover every Grid member (host_name
#                                          plus selected member info).
#   2. GET /wapi/<ver>/capacityreport  -> per-member capacity numbers
#                                          (max_capacity, total_objects,
#                                          percent_used, per-object-type counts).
#
# Inputs can be supplied on the command line OR in a .env file. Precedence is:
#   command line  >  .env file  >  (for the password only) secure prompt.
#
# Author: Pat Vogelsang
# Year:   2026
#
# -----------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2026 Pat Vogelsang
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -----------------------------------------------------------------------------
"""Collect NIOS Grid member capacity via WAPI and export it to CSV.

WHAT THIS SCRIPT DOES
---------------------
Given the IP address or FQDN of a NIOS Grid Manager and a set of API
credentials, this script:

  * Calls the WAPI ``member`` object to enumerate every Grid member and gather
    useful member information (host name, node hardware type/model, HA status,
    hypervisor/platform, management port and DNS resolver settings, etc.).
  * Calls the WAPI ``capacityreport`` object once per member to retrieve that
    member's capacity numbers (hardware type, maximum object capacity, total
    objects in use, percent used, role) plus the per-object-type counts.
  * Writes everything to a single CSV file with exactly one row per member.

WHERE INPUTS COME FROM
----------------------
Every input may be provided on the command line OR in a ``.env`` file. The
resolution order (highest priority first) is:

  1. Command-line argument (e.g. ``--grid-manager 192.168.1.1``).
  2. Matching key in the ``.env`` file (e.g. ``NIOS_GRID_MANAGER=192.168.1.1``).
  3. For the PASSWORD only: a secure, no-echo interactive prompt.

The ``.env`` keys are: NIOS_GRID_MANAGER, NIOS_USERNAME, NIOS_PASSWORD,
NIOS_OUTPUT, NIOS_WAPI_VERSION, NIOS_PAGE_SIZE, NIOS_TIMEOUT, NIOS_CA_CERT,
NIOS_INSECURE.

OUTPUTS
-------
  * A CSV file at the resolved output path. Member-info columns come first, then
    the capacity summary columns, then one ``obj_<type>`` column for every
    object type reported by any member (the UNION across all members so nothing
    is lost). Missing values are left blank.

DEPENDENCIES
------------
  * Python 3.8+ and the standard library ONLY. NIOS WAPI is plain HTTPS with
    HTTP Basic authentication, which ``urllib`` handles natively, so no
    third-party HTTP client (such as ``requests``) is required. The ``.env``
    file is parsed with a tiny built-in parser, so ``python-dotenv`` is NOT
    needed either.
"""

# --- Standard library imports (no third-party packages needed) ---
import argparse            # command-line parsing and auto-generated --help
import base64              # to build the HTTP Basic auth header manually
import csv                 # to write the results file
import getpass             # to prompt for the password without echoing it
import json                # to parse WAPI JSON responses
import logging             # structured status/diagnostic output (wired to -v)
import os                  # to read environment variables and check permissions
import ssl                 # to build the TLS context (NIOS often uses self-signed certs)
import sys                 # for clean, non-zero exits on error
import urllib.error        # to catch HTTP/URL errors cleanly
import urllib.parse        # to safely build query strings
import urllib.request      # to perform the HTTPS GET requests
from datetime import datetime
from pathlib import Path   # for robust, cross-platform path handling


# A module-level logger. Verbosity (INFO vs DEBUG) is configured in main()
# based on the --verbose flag so the same logger is used everywhere.
LOG = logging.getLogger("nios_grid_capacity")

# Maps the .env file keys to the internal setting names the script uses. Keeping
# this in one place makes the precedence logic and the documentation consistent.
ENV_KEYS = {
    "NIOS_GRID_MANAGER": "grid_manager",
    "NIOS_USERNAME": "username",
    "NIOS_PASSWORD": "password",
    "NIOS_OUTPUT": "output",
    "NIOS_WAPI_VERSION": "wapi_version",
    "NIOS_PAGE_SIZE": "page_size",
    "NIOS_TIMEOUT": "timeout",
    "NIOS_CA_CERT": "ca_cert",
    "NIOS_INSECURE": "insecure",
    "NIOS_VERBOSE_OUTPUT": "verbose_output",
}

# Built-in defaults applied only when a setting is given neither on the command
# line nor in the .env file.
DEFAULTS = {
    "wapi_version": "v2.14",
    "page_size": 1000,
    "timeout": 60.0,
    "insecure": False,
    "ca_cert": None,
    "verbose_output": False,
}


# =============================================================================
# --- Argument parsing ---
# =============================================================================
def parse_args(argv=None):
    """Define and parse command-line arguments.

    Note:
        Required inputs (grid manager, username, output) are intentionally NOT
        marked ``required`` here, because they may instead be supplied through
        the ``.env`` file. Their presence is validated later in
        ``resolve_config`` once the command line and ``.env`` have been merged.
        Optional settings default to ``None`` so we can tell "user did not set
        this" apart from "user chose the default value", which is what makes the
        command-line-over-.env precedence work correctly.

    Args:
        argv (list[str] | None): argument vector (defaults to sys.argv).

    Returns:
        argparse.Namespace: the parsed arguments (values may be None).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Collect capacity information for every member of an Infoblox NIOS "
            "Grid via WAPI and write it to a CSV file (one row per member). "
            "Inputs may come from the command line or a .env file (command line "
            "wins; the password can also be prompted for securely)."
        ),
        epilog=(
            "Input precedence: command line > .env file > (password) secure prompt.\n\n"
            ".env file keys (KEY=value, one per line):\n"
            "  NIOS_GRID_MANAGER, NIOS_USERNAME, NIOS_PASSWORD, NIOS_OUTPUT,\n"
            "  NIOS_WAPI_VERSION, NIOS_PAGE_SIZE, NIOS_TIMEOUT, NIOS_CA_CERT,\n"
            "  NIOS_INSECURE, NIOS_VERBOSE_OUTPUT\n\n"
            "Examples:\n"
            "  # All inputs on the command line, prompt for the password:\n"
            "  ./nios_grid_capacity.py --grid-manager 192.168.1.1 \\\n"
            "      --username admin --output grid_capacity.csv\n\n"
            "  # All inputs (incl. password) in a .env file in the current dir:\n"
            "  ./nios_grid_capacity.py            # reads ./.env automatically\n\n"
            "  # .env for host/user, but override the output on the command line:\n"
            "  ./nios_grid_capacity.py --env-file prod.env --output prod_cap.csv\n\n"
            "  # Lab grid with a self-signed certificate (skip TLS verification):\n"
            "  ./nios_grid_capacity.py -g 192.168.1.1 -u admin -o out.csv --insecure\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Where to read the .env file from ---
    parser.add_argument(
        "--env-file",
        default=".env",
        metavar="ENV_FILE",
        help=(
            "Path to a .env file holding any of the NIOS_* keys (default: .env "
            "in the current directory). It is used only for values not supplied "
            "on the command line. If the file does not exist it is simply "
            "ignored (unless you pass a custom path that is missing)."
        ),
    )

    # --- Connection / target arguments (optional here; validated after merge) ---
    parser.add_argument(
        "-g", "--grid-manager",
        default=None,
        metavar="IP_OR_FQDN",
        help="IP address or FQDN of the NIOS Grid Manager. (.env: NIOS_GRID_MANAGER)",
    )
    parser.add_argument(
        "-u", "--username",
        default=None,
        metavar="USERNAME",
        help="WAPI (NIOS admin) username. (.env: NIOS_USERNAME)",
    )
    parser.add_argument(
        "-p", "--password",
        default=None,
        metavar="PASSWORD",
        help=(
            "WAPI password. For security, prefer NOT to pass this on the command "
            "line; put it in the .env file (NIOS_PASSWORD) or let the script "
            "prompt you securely."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        metavar="OUTPUT_CSV",
        help="Path where the resulting CSV file will be written. (.env: NIOS_OUTPUT)",
    )

    # --- Optional WAPI / behavior arguments (default None so .env can win) ---
    parser.add_argument(
        "--wapi-version",
        default=None,
        metavar="VERSION",
        help="WAPI version segment used in the URL path (default: v2.14). (.env: NIOS_WAPI_VERSION)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Members to request per page when enumerating (default: 1000; must "
            "be a positive integer). (.env: NIOS_PAGE_SIZE)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-request network timeout in seconds (default: 60). (.env: NIOS_TIMEOUT)",
    )

    # --- TLS / certificate handling ---
    parser.add_argument(
        "--ca-cert",
        default=None,
        metavar="CA_BUNDLE",
        help=(
            "PEM CA bundle used to verify the Grid Manager's TLS certificate. "
            "(.env: NIOS_CA_CERT)"
        ),
    )
    # store_true defaults to False; we cannot tell "not passed" from "passed",
    # so we treat CLI True as an override and otherwise fall back to .env.
    parser.add_argument(
        "-i","--insecure",
        action="store_true",
        help=(
            "Disable TLS certificate verification (handy for lab grids with "
            "self-signed certs, INSECURE for production). (.env: NIOS_INSECURE=true)"
        ),
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging for troubleshooting.",
    )
    parser.add_argument(
        "--verbose-output",
        action="store_true",
        help=(
            "Include the verbose-only CSV columns in the output and write both "
            "the original and renamed header rows. (.env: NIOS_VERBOSE_OUTPUT=true)"
        ),
    )

    return parser.parse_args(argv)


# =============================================================================
# --- .env parsing & configuration resolution ---
# =============================================================================
def load_env_file(env_path, explicitly_requested):
    """Parse a simple ``.env`` file into a dictionary.

    The parser is intentionally minimal and dependency-free. It supports:
      * ``KEY=value`` lines.
      * Blank lines and ``#`` comment lines (ignored).
      * An optional leading ``export `` (as commonly seen in shell env files).
      * Values optionally wrapped in single or double quotes (quotes stripped).
      * Inline ``#`` comments on UNQUOTED values (everything after ``#`` dropped).

    Args:
        env_path (str): path to the .env file.
        explicitly_requested (bool): True if the user passed a custom
            ``--env-file`` path. When True, a missing file is an error; when
            False (the default ``.env``), a missing file is silently ignored.

    Returns:
        dict: mapping of NIOS_* keys found in the file to their string values.
              Only recognized keys (see ENV_KEYS) are returned; unknown keys are
              ignored with a debug log so typos are visible under -v.

    Raises:
        SystemExit: if an explicitly requested env file does not exist, or if a
            line is malformed.
    """
    path = Path(env_path)

    if not path.is_file():
        # A missing DEFAULT .env is normal (the user may pass everything on the
        # command line). A missing EXPLICIT --env-file is a mistake worth flagging.
        if explicitly_requested:
            sys.exit(f"Error: --env-file was given but the file does not exist: {env_path}")
        LOG.debug("No .env file found at %s; relying on command-line inputs.", env_path)
        return {}

    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for lineno, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()

                # Skip blank lines and full-line comments.
                if not line or line.startswith("#"):
                    continue

                # Allow an optional "export " prefix.
                if line.startswith("export "):
                    line = line[len("export "):].strip()

                # Every meaningful line must be KEY=VALUE.
                if "=" not in line:
                    sys.exit(
                        f"Error: malformed line {lineno} in {env_path} "
                        f"(expected KEY=value): {raw_line.rstrip()}"
                    )

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Handle quoted values (keep '#' inside quotes literally).
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                else:
                    # For unquoted values, strip trailing inline comments.
                    hash_index = value.find(" #")
                    if hash_index != -1:
                        value = value[:hash_index].strip()

                # Only keep keys we understand; warn (debug) about the rest.
                if key in ENV_KEYS:
                    values[key] = value
                else:
                    LOG.debug("Ignoring unrecognized .env key on line %d: %s", lineno, key)
    except OSError as err:
        sys.exit(f"Error reading --env-file {env_path}: {err}")

    LOG.debug("Loaded %d recognized key(s) from %s", len(values), env_path)
    return values


def _coerce_int(name, value, source):
    """Convert a string to int with a clear error message. (Helper.)"""
    try:
        return int(value)
    except (TypeError, ValueError):
        sys.exit(f"Error: {name} from {source} must be an integer, got: {value!r}")


def _coerce_float(name, value, source):
    """Convert a string to float with a clear error message. (Helper.)"""
    try:
        return float(value)
    except (TypeError, ValueError):
        sys.exit(f"Error: {name} from {source} must be a number, got: {value!r}")


def _coerce_bool(value):
    """Interpret a .env string as a boolean (true/1/yes/on -> True). (Helper.)"""
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def resolve_config(args, env):
    """Merge command-line args and .env values, apply defaults, and validate.

    Precedence for every setting: command line > .env > built-in default.
    (The password is handled separately in ``resolve_password`` because it also
    supports an interactive prompt as a last resort.)

    Args:
        args (argparse.Namespace): parsed command-line arguments.
        env (dict): recognized NIOS_* values from the .env file.

    Returns:
        dict: the fully-resolved, validated configuration.

    Raises:
        SystemExit: on any missing required value or invalid input.
    """
    config = {}

    # --- Strings: grid_manager, username, output, wapi_version, ca_cert ---
    config["grid_manager"] = args.grid_manager or env.get("NIOS_GRID_MANAGER")
    config["username"] = args.username or env.get("NIOS_USERNAME")
    config["output"] = args.output or env.get("NIOS_OUTPUT")
    config["wapi_version"] = (
        args.wapi_version or env.get("NIOS_WAPI_VERSION") or DEFAULTS["wapi_version"]
    )
    config["ca_cert"] = args.ca_cert or env.get("NIOS_CA_CERT") or DEFAULTS["ca_cert"]

    # --- Numbers: page_size, timeout (convert .env strings as needed) ---
    if args.page_size is not None:
        config["page_size"] = args.page_size
    elif "NIOS_PAGE_SIZE" in env:
        config["page_size"] = _coerce_int("page_size", env["NIOS_PAGE_SIZE"], ".env")
    else:
        config["page_size"] = DEFAULTS["page_size"]

    if args.timeout is not None:
        config["timeout"] = args.timeout
    elif "NIOS_TIMEOUT" in env:
        config["timeout"] = _coerce_float("timeout", env["NIOS_TIMEOUT"], ".env")
    else:
        config["timeout"] = DEFAULTS["timeout"]

    # --- Boolean: insecure (CLI flag True overrides; else .env; else default) ---
    if args.insecure:
        config["insecure"] = True
    elif "NIOS_INSECURE" in env:
        config["insecure"] = _coerce_bool(env["NIOS_INSECURE"])
    else:
        config["insecure"] = DEFAULTS["insecure"]

    if args.verbose_output:
        config["verbose_output"] = True
    elif "NIOS_VERBOSE_OUTPUT" in env:
        config["verbose_output"] = _coerce_bool(env["NIOS_VERBOSE_OUTPUT"])
    else:
        config["verbose_output"] = DEFAULTS["verbose_output"]

    # --- Validate required values are present from some source ---
    missing = [
        label
        for label, value in (
            ("Grid Manager (-g / --grid-manager or NIOS_GRID_MANAGER)", config["grid_manager"]),
            ("username (-u / --username or NIOS_USERNAME)", config["username"]),
            ("output path (-o / --output or NIOS_OUTPUT)", config["output"]),
        )
        if not value
    ]
    if missing:
        sys.exit(
            "Error: the following required input(s) were not provided on the "
            "command line or in the .env file:\n  - " + "\n  - ".join(missing)
        )

    # --- Validate value ranges / files, exactly as before ---
    if config["page_size"] <= 0:
        sys.exit(f"Error: page_size must be a positive integer, got: {config['page_size']}")
    if config["timeout"] <= 0:
        sys.exit(f"Error: timeout must be greater than zero, got: {config['timeout']}")

    if config["ca_cert"] is not None:
        ca_path = Path(config["ca_cert"])
        if not ca_path.is_file():
            sys.exit(f"Error: ca_cert file does not exist or is not a file: {config['ca_cert']}")

    # --ca-cert and --insecure are contradictory.
    if config["ca_cert"] is not None and config["insecure"]:
        sys.exit("Error: a CA certificate and 'insecure' (skip verification) cannot both be set.")

    # Output directory must exist and be writable, so we fail before doing work.
    out_path = Path(config["output"])
    out_dir = out_path.parent if str(out_path.parent) != "" else Path(".")
    if not out_dir.is_dir():
        sys.exit(f"Error: output directory does not exist: {out_dir}")
    if not os.access(out_dir, os.W_OK):
        sys.exit(f"Error: output directory is not writable: {out_dir}")

    return config


def resolve_password(args, env):
    """Determine the WAPI password following the required precedence.

    Order (highest priority first):
        1. ``--password`` command-line argument.
        2. ``NIOS_PASSWORD`` from the .env file.
        3. ``NIOS_PASSWORD`` from the actual process environment (convenience).
        4. Secure interactive prompt via ``getpass`` (no echo / obscured).

    Args:
        args (argparse.Namespace): parsed arguments (uses ``args.password``).
        env (dict): recognized NIOS_* values from the .env file.

    Returns:
        str: the resolved password.

    Raises:
        SystemExit: if an empty password is provided at the prompt.
    """
    # 1) Command line wins.
    if args.password:
        LOG.debug("Using password supplied via --password (not recommended).")
        return args.password

    # 2) .env file.
    if env.get("NIOS_PASSWORD"):
        LOG.debug("Using password from the .env file (NIOS_PASSWORD).")
        return env["NIOS_PASSWORD"]

    # 3) Real environment variable, as a convenience for CI/automation.
    os_password = os.environ.get("NIOS_PASSWORD")
    if os_password:
        LOG.debug("Using password from the NIOS_PASSWORD environment variable.")
        return os_password

    # 4) Prompt securely; getpass does not echo the characters typed.
    username = args.username or env.get("NIOS_USERNAME") or "the WAPI user"
    try:
        prompted = getpass.getpass(f"WAPI password for {username}: ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted: no password provided.")

    if not prompted:
        sys.exit("Error: an empty password was provided; cannot authenticate.")
    return prompted


# =============================================================================
# --- WAPI client helpers ---
# =============================================================================
def build_ssl_context(insecure, ca_cert):
    """Create an ``ssl.SSLContext`` based on the resolved TLS settings.

    Args:
        insecure (bool): if True, disable certificate/hostname verification.
        ca_cert (str | None): optional path to a PEM CA bundle.

    Returns:
        ssl.SSLContext: a context ready to be passed to urllib.
    """
    if insecure:
        # Explicitly requested: turn off hostname checking and cert validation.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if ca_cert:
        # Verify against the user-supplied CA bundle only.
        return ssl.create_default_context(cafile=ca_cert)

    # Default: verify using the system's trusted CA store.
    return ssl.create_default_context()


def wapi_get(base_url, path, params, auth_header, ssl_context, timeout):
    """Perform a single authenticated WAPI GET request and return parsed JSON.

    Args:
        base_url (str): e.g. ``https://gm.example.com/wapi/v2.14``.
        path (str): object path appended to base_url, e.g. ``member``.
        params (dict): query-string parameters (values may be str/int).
        auth_header (str): a ready-to-use ``Authorization`` header value.
        ssl_context (ssl.SSLContext): TLS context for the connection.
        timeout (float): per-request timeout in seconds.

    Returns:
        object: the decoded JSON body (typically a list or dict).

    Raises:
        RuntimeError: on HTTP errors, connection failures, or invalid JSON,
            with a human-readable explanation of what went wrong.
    """
    # Build the full URL with a properly-encoded query string.
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{base_url}/{path}"
    if query:
        url = f"{url}?{query}"

    LOG.debug("GET %s", url)

    # Construct the request with Basic auth and a JSON Accept header.
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", auth_header)
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            raw = response.read()
    except urllib.error.HTTPError as http_err:
        # WAPI returns useful JSON error bodies; surface them to the user.
        detail = ""
        try:
            detail = http_err.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best-effort detail extraction only
            pass
        if http_err.code == 401:
            raise RuntimeError(
                "Authentication failed (HTTP 401): check the username and "
                "password for the Grid Manager."
            ) from http_err
        raise RuntimeError(
            f"WAPI request failed (HTTP {http_err.code} {http_err.reason}) for {url}. "
            f"Server said: {detail.strip() or '(no body)'}"
        ) from http_err
    except urllib.error.URLError as url_err:
        # Covers DNS failures, refused connections, TLS problems, timeouts, etc.
        reason = getattr(url_err, "reason", url_err)
        raise RuntimeError(
            f"Could not connect to the Grid Manager at {url}: {reason}. "
            "Verify the Grid Manager address, network reachability, and (for "
            "self-signed certificates) whether you need --insecure or --ca-cert."
        ) from url_err

    # Decode the JSON body. NIOS returns UTF-8 JSON.
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as decode_err:
        raise RuntimeError(
            f"Received a response from {url} that was not valid JSON: {decode_err}"
        ) from decode_err


def fetch_all_members(base_url, auth_header, ssl_context, timeout, page_size):
    """Enumerate every Grid member, following WAPI paging as needed.

    Uses the exact member query supplied in the API examples:
        member?_return_fields=host_name,dns_resolver_setting,syslog_servers,
                additional_ip_list,mgmt_port_setting,node_info

    Paging is enabled via ``_paging=1`` + ``_return_as_object=1`` so large grids
    are fully enumerated rather than silently truncated.

    Args:
        base_url (str): WAPI base URL.
        auth_header (str): Authorization header value.
        ssl_context (ssl.SSLContext): TLS context.
        timeout (float): per-request timeout.
        page_size (int): number of members per page.

    Returns:
        list[dict]: all member objects returned by WAPI.
    """
    return_fields = (
        "host_name,dns_resolver_setting,syslog_servers,"
        "additional_ip_list,mgmt_port_setting,node_info"
    )

    members = []
    page_id = None  # WAPI paging cursor; None on the first request.

    while True:
        params = {
            "_return_fields": return_fields,
            "_max_results": page_size,
            "_paging": 1,
            "_return_as_object": 1,
        }
        if page_id:
            params["_page_id"] = page_id

        payload = wapi_get(base_url, "member", params, auth_header, ssl_context, timeout)

        # With _return_as_object=1 the body is a dict: {"result": [...],
        # "next_page_id": "..."}. Be defensive in case a grid returns a bare list.
        if isinstance(payload, dict):
            batch = payload.get("result", [])
            page_id = payload.get("next_page_id")
        else:
            batch = payload
            page_id = None

        members.extend(batch)
        LOG.debug("Fetched %d members so far.", len(members))

        if not page_id:
            break

    return members


def fetch_member_capacity(base_url, member_name, auth_header, ssl_context, timeout):
    """Retrieve the capacity report for a single member by name.

    Uses the exact capacity query supplied in the API examples:
        capacityreport?name=<member_name>&_return_fields=object_counts,
                total_objects,hardware_type,max_capacity,name,role,percent_used

    Args:
        base_url (str): WAPI base URL.
        member_name (str): the member's host_name.
        auth_header (str): Authorization header value.
        ssl_context (ssl.SSLContext): TLS context.
        timeout (float): per-request timeout.

    Returns:
        dict | None: the capacity report dict for the member, or None if the
            Grid Manager returned no capacity report for that name.
    """
    return_fields = (
        "object_counts,total_objects,hardware_type,"
        "max_capacity,name,role,percent_used"
    )
    params = {"name": member_name, "_return_fields": return_fields}

    payload = wapi_get(base_url, "capacityreport", params, auth_header, ssl_context, timeout)

    if isinstance(payload, list) and payload:
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return None


# =============================================================================
# --- Data flattening (turn nested JSON into flat CSV columns) ---
# =============================================================================
def flatten_member_info(member):
    """Flatten a member object's nested info into simple key/value columns.

    NIOS members can have one or two nodes (HA pairs), so node-level fields are
    emitted as ``node1_*`` and ``node2_*`` columns.

    Args:
        member (dict): a single member object from the ``member`` query.

    Returns:
        dict: flat mapping of member-info column names to string/number values.
    """
    row = {}

    # Basic identity and reference.
    row["host_name"] = member.get("host_name", "")
    row["member_ref"] = member.get("_ref", "")

    # Management port settings.
    mgmt = member.get("mgmt_port_setting") or {}
    row["mgmt_enabled"] = mgmt.get("enabled", "")
    row["mgmt_vpn_enabled"] = mgmt.get("vpn_enabled", "")
    row["mgmt_security_access_enabled"] = mgmt.get("security_access_enabled", "")

    # DNS resolver settings: join lists into readable, semicolon-separated strings.
    resolver = member.get("dns_resolver_setting") or {}
    row["dns_resolvers"] = ";".join(resolver.get("resolvers", []) or [])
    row["dns_search_domains"] = ";".join(resolver.get("search_domains", []) or [])

    # Syslog servers: capture how many are configured and their addresses.
    syslog_servers = member.get("syslog_servers") or []
    row["syslog_server_count"] = len(syslog_servers)
    row["syslog_server_addresses"] = ";".join(
        str(s.get("address", "")) for s in syslog_servers if isinstance(s, dict)
    )

    # Additional IPs configured on the member.
    additional_ips = member.get("additional_ip_list") or []
    row["additional_ip_count"] = len(additional_ips)

    # Per-node hardware / status details. Emit node1_* and node2_* columns.
    node_info = member.get("node_info") or []
    for index, node in enumerate(node_info[:2], start=1):
        prefix = f"node{index}_"
        row[f"{prefix}ha_status"] = node.get("ha_status", "")
        row[f"{prefix}host_platform"] = node.get("host_platform", "")
        row[f"{prefix}hwid"] = node.get("hwid", "")
        row[f"{prefix}hwmodel"] = node.get("hwmodel", "")
        row[f"{prefix}hwtype"] = node.get("hwtype", "")
        row[f"{prefix}hypervisor"] = node.get("hypervisor", "")
        row[f"{prefix}paid_nios"] = node.get("paid_nios", "")

        # Summarize each node's service_status list into "service=status" pairs.
        services = node.get("service_status") or []
        row[f"{prefix}service_status"] = ";".join(
            f"{s.get('service', '')}={s.get('status', '')}"
            for s in services if isinstance(s, dict)
        )

    return row


def estimate_uddi_objects(object_counts):
    """Estimate how many DDI and Active IP objects a member would use.

    The estimate is based on an explicit mapping of capacity object types to one
    of two buckets: DDI Object or Active IP. The function returns three values:
    the DDI-object total, the Active IP total, and the combined UDDI total.
    """
    ddi_types = {
        "A Record/Substitute (A Record) Rule/Substitute (IPv4 Address) Rule",
        "Access Control Item",
        "CNAME Record/Substitute Domain Name/Block/Passthru Rule",
        "DHCP Custom Option",
        "DHCP Range",
        "DNS Traffic Control HTTP Monitor",
        "DNS Traffic Control ICMP Monitor",
        "DNS Traffic Control PDP Monitor",
        "DNS Traffic Control SIP Monitor",
        "DNS Traffic Control SNMP Monitor",
        "Host Alias",
        "Network",
        "Network Container",
        "PTR Record/Substitute (PTR Record) Rule",
        "Router",
        "SVCB Record/Substitute (SVCB Record) Rule",
        "TXT Record/Substitute (TXT Record) Rule",
        "View",
        "Zone",
        "Zone SOA",
    }
    active_ip_types = {
        "Fixed Address",
        "Host",
        "Host Address",
    }

    ddi_total = 0
    active_ip_total = 0
    for entry in object_counts or []:
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type_name", "")
        if type_name in ddi_types:
            try:
                ddi_total += int(entry.get("count", 0))
            except (TypeError, ValueError):
                continue
        elif type_name in active_ip_types:
            try:
                active_ip_total += int(entry.get("count", 0))
            except (TypeError, ValueError):
                continue

    return ddi_total, active_ip_total, ddi_total + active_ip_total


def flatten_capacity(capacity):
    """Flatten a capacity report into summary columns plus per-object counts.

    Args:
        capacity (dict | None): the capacity report for a member (or None).

    Returns:
        tuple[dict, dict]:
            * summary: fixed capacity columns (max_capacity, total_objects, ...).
            * object_counts: mapping of ``obj_<type_name>`` -> count.
    """
    summary = {
        "cap_name": "",
        "cap_role": "",
        "cap_hardware_type": "",
        "cap_max_capacity": "",
        "cap_total_objects": "",
        "cap_percent_used": "",
        "cap_uddi_ddi_objects": "",
        "cap_uddi_active_ip_objects": "",
        "cap_uddi_total_objects": "",
        "cap_report_found": False,
    }
    object_counts = {}

    if not capacity:
        return summary, object_counts

    summary["cap_name"] = capacity.get("name", "")
    summary["cap_role"] = capacity.get("role", "")
    summary["cap_hardware_type"] = capacity.get("hardware_type", "")
    summary["cap_max_capacity"] = capacity.get("max_capacity", "")
    summary["cap_total_objects"] = capacity.get("total_objects", "")
    summary["cap_percent_used"] = capacity.get("percent_used", "")
    summary["cap_report_found"] = True

    # Expand each object-count entry into its own column so ALL capacity numbers
    # are captured. Column names are prefixed with "obj_" to avoid colliding
    # with the member-info/summary columns.
    for entry in capacity.get("object_counts", []) or []:
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type_name", "")
        if type_name == "":
            continue
        object_counts[f"obj_{type_name}"] = entry.get("count", "")

    ddi_total, active_ip_total, combined_total = estimate_uddi_objects(
        capacity.get("object_counts", []) or []
    )
    summary["cap_uddi_ddi_objects"] = ddi_total
    summary["cap_uddi_active_ip_objects"] = active_ip_total
    summary["cap_uddi_total_objects"] = combined_total

    return summary, object_counts


# =============================================================================
# --- CSV writing ---
# =============================================================================
def normalize_header_name(column):
    for prefix in ("cap_", "obj_"):
        if column.startswith(prefix):
            return column[len(prefix):]
    return column

def build_unique_output_path(output_path, verbose_output=False):
    """Return a unique output path by adding a timestamped suffix.

    The base file name is preserved, a timestamp is inserted before the
    extension, and an incrementing counter is appended if the file already
    exists to guarantee uniqueness.
    """
    path = Path(output_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    if path.suffix:
        stem = path.stem
        suffix = path.suffix
    else:
        stem = path.name
        suffix = ""

    if verbose_output:
        stem = f"{stem}_verbose"

    candidate = path.with_name(f"{stem}_{timestamp}{suffix}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{stem}_{timestamp}_{counter}{suffix}")
        counter += 1

    return candidate


def load_header_layout(layout_path=None):
    """Load the original/new header mapping and verbose flags from newheadings.csv."""
    path = Path(layout_path or Path(__file__).with_name("newheadings.csv"))
    if not path.is_file():
        return {}

    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
    except OSError as err:
        LOG.debug("Could not read header layout file %s: %s", path, err)
        return {}

    if len(rows) < 3:
        return {}

    original_names = [cell.strip() for cell in rows[0][1:]]
    verbose_flags = [cell.strip() for cell in rows[1][1:]]
    renamed_names = [cell.strip() for cell in rows[2][1:]]

    layout = {}
    for original_name, verbose_flag, new_name in zip(original_names, verbose_flags, renamed_names):
        if not original_name:
            continue
        layout[original_name] = {
            "new_name": new_name or original_name,
            "verbose": verbose_flag == "VERBOSE",
        }
    return layout


def select_output_columns(columns, verbose_output, layout_path=None):
    """Return the columns and their display headers for the CSV export."""
    layout = load_header_layout(layout_path)
    selected_columns = []
    selected_headers = []

    for column in columns:
        definition = layout.get(column)
        if definition and definition["verbose"] and not verbose_output:
            continue
        selected_columns.append(column)
        if definition:
            selected_headers.append(definition["new_name"])
        else:
            selected_headers.append(normalize_header_name(column))

    return selected_columns, selected_headers

def write_csv(output_path, rows, member_info_keys, summary_keys, object_count_keys, verbose_output=False):
    """Write the collected rows to a CSV file with original/new header rows.

    The output includes two header rows: the first row uses the script's original
    field names, and the second row uses the new display names from
    newheadings.csv. Verbose-only columns are omitted unless the user requested
    verbose output.
    """
    ordered_object_keys = sorted(object_count_keys)
    fieldnames = list(member_info_keys) + list(summary_keys) + ordered_object_keys
    selected_columns, selected_headers = select_output_columns(
        fieldnames, verbose_output
    )

    # newline="" is required by the csv module to avoid blank lines on Windows.
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(selected_headers)
        for row in rows:
            writer.writerow([row.get(key, "") for key in selected_columns])


# =============================================================================
# --- Main orchestration ---
# =============================================================================
def main():
    """Entry point: resolve inputs, talk to WAPI, and write the CSV."""
    args = parse_args()

    # Configure logging early: DEBUG when --verbose, otherwise INFO. Messages go
    # to stderr so they never contaminate the CSV or any piped stdout.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stderr,
    )

    # Load the .env file (if any). A custom --env-file that is missing is an
    # error; a missing default ./.env is silently ignored.
    explicitly_requested = (args.env_file != ".env")
    env = load_env_file(args.env_file, explicitly_requested)

    # Merge command line + .env + defaults, then validate everything.
    config = resolve_config(args, env)

    # Prominent warning when TLS verification is disabled.
    if config["insecure"]:
        LOG.warning(
            "TLS certificate verification is DISABLED (insecure). This is fine "
            "for lab grids but should not be used against production systems."
        )

    # Resolve the password: command line > .env > OS env > secure prompt.
    password = resolve_password(args, env)

    # Pre-compute the Basic auth header once (base64 of "user:password").
    token = base64.b64encode(
        f"{config['username']}:{password}".encode("utf-8")
    ).decode("ascii")
    auth_header = f"Basic {token}"

    # Build the WAPI base URL, accepting either a bare host or a full URL and
    # normalizing to https://<host>/wapi/<version>.
    grid_host = config["grid_manager"].strip().rstrip("/")
    for scheme in ("https://", "http://"):
        if grid_host.lower().startswith(scheme):
            grid_host = grid_host[len(scheme):]
    base_url = f"https://{grid_host}/wapi/{config['wapi_version']}"
    LOG.info("Using WAPI base URL: %s", base_url)

    ssl_context = build_ssl_context(config["insecure"], config["ca_cert"])

    # --- Step 1: enumerate all members ---
    LOG.info("Querying the Grid Manager for all members...")
    try:
        members = fetch_all_members(
            base_url, auth_header, ssl_context, config["timeout"], config["page_size"]
        )
    except RuntimeError as err:
        sys.exit(f"Error while listing members: {err}")

    if not members:
        sys.exit("No Grid members were returned by the WAPI 'member' query. Nothing to do.")
    LOG.info("Found %d Grid member(s).", len(members))

    # --- Step 2: for each member, fetch its capacity report and flatten it ---
    rows = []
    object_count_keys = set()          # union of every obj_<type> column seen
    member_info_key_order = None       # preserve first member's info-column order
    missing_capacity = []              # members with no capacity report

    for member in members:
        info = flatten_member_info(member)
        host_name = info.get("host_name", "")

        # Track member-info column order, extending it if later HA members add
        # node2_* columns the first member didn't have.
        if member_info_key_order is None:
            member_info_key_order = list(info.keys())
        else:
            for key in info.keys():
                if key not in member_info_key_order:
                    member_info_key_order.append(key)

        if not host_name:
            LOG.warning("Skipping a member with no host_name in its record.")
            continue

        LOG.info("Fetching capacity for member: %s", host_name)
        try:
            capacity = fetch_member_capacity(
                base_url, host_name, auth_header, ssl_context, config["timeout"]
            )
        except RuntimeError as err:
            # One member failing should not abort the whole run.
            LOG.warning("Could not fetch capacity for %s: %s", host_name, err)
            capacity = None

        summary, obj_counts = flatten_capacity(capacity)
        if not summary["cap_report_found"]:
            missing_capacity.append(host_name)

        object_count_keys.update(obj_counts.keys())

        combined = {}
        combined.update(info)
        combined.update(summary)
        combined.update(obj_counts)
        rows.append(combined)

    # Capacity summary columns in a deliberate, readable order.
    summary_keys = [
        "cap_report_found",
        "cap_name",
        "cap_role",
        "cap_hardware_type",
        "cap_max_capacity",
        "cap_total_objects",
        "cap_percent_used",
        "cap_uddi_ddi_objects",
        "cap_uddi_active_ip_objects",
        "cap_uddi_total_objects",
    ]

    # --- Step 3: write everything out to CSV ---
    output_path = build_unique_output_path(config["output"], verbose_output=config["verbose_output"])
    config["output"] = str(output_path)

    try:
        write_csv(
            config["output"],
            rows,
            member_info_key_order,
            summary_keys,
            object_count_keys,
            verbose_output=config["verbose_output"],
        )
    except OSError as err:
        sys.exit(f"Error writing CSV to {config['output']}: {err}")

    LOG.info("Wrote %d member row(s) to %s", len(rows), config["output"])
    if missing_capacity:
        LOG.warning(
            "No capacity report was returned for %d member(s): %s",
            len(missing_capacity),
            ", ".join(missing_capacity),
        )
    print(f"Done. CSV written to: {config['output']}")


# Guard the entry point so the module can also be imported without side effects.
if __name__ == "__main__":
    main()
