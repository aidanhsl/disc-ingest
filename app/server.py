#!/usr/bin/env python3
"""Small, dependency-free web controller for MakeMKV disc ingestion."""

from __future__ import annotations

import base64
import csv
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/mnt/data")).resolve()
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config")).resolve()
STAGING_ROOT = MEDIA_ROOT / ".disc-ingest"
MAKEMKV_BIN = os.environ.get("MAKEMKV_BIN", "/opt/makemkv/bin/makemkvcon")
MAKEMKV_WRAPPER = os.environ.get("MAKEMKV_WRAPPER", "/opt/makemkv/lib/libwrapper.so")
EJECT_BIN = os.environ.get("EJECT_BIN", "/usr/bin/eject")
OPTICAL_BLOCK_DEVICE = os.environ.get("OPTICAL_BLOCK_DEVICE", "/dev/sr0")
AUTO_EJECT = os.environ.get("AUTO_EJECT", "1").lower() in {"1", "true", "yes", "on"}
WEB_USERNAME = os.environ.get("WEB_USERNAME", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
MAX_LOG_LINES = 240
MAX_REQUEST_BYTES = 32 * 1024


class ValidationError(ValueError):
    pass


@dataclass
class TitleInfo:
    id: int
    duration: str = "Unknown"
    seconds: int = 0
    size: str = "Unknown"
    bytes: int = 0
    chapters: int = 0
    filename: str = ""
    name: str = ""


@dataclass
class DiscInfo:
    name: str
    titles: list[TitleInfo]
    movie_default: list[int]
    show_default: list[int]


def robot_fields(line: str, prefix: str) -> list[str]:
    return next(csv.reader([line[len(prefix) :]], escapechar="\\"))


def duration_seconds(value: str) -> int:
    parts = value.strip().split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def episode_candidates(titles: list[TitleInfo]) -> list[int]:
    plausible = [title for title in titles if 10 * 60 <= title.seconds <= 2 * 60 * 60]
    if not plausible:
        return [title.id for title in titles if title.seconds >= 10 * 60]

    best_group: list[TitleInfo] = []
    for anchor in plausible:
        tolerance = max(180, int(anchor.seconds * 0.12))
        group = [title for title in plausible if abs(title.seconds - anchor.seconds) <= tolerance]
        if len(group) > len(best_group):
            best_group = group

    selected = best_group if len(best_group) >= 2 else plausible
    return [title.id for title in sorted(selected, key=lambda item: item.id)]


def parse_disc_info(output: str) -> DiscInfo | None:
    disc_name = "Untitled disc"
    title_map: dict[int, TitleInfo] = {}

    for raw_line in output.splitlines():
        line = raw_line.strip()
        try:
            if line.startswith("CINFO:"):
                fields = robot_fields(line, "CINFO:")
                if len(fields) >= 3 and int(fields[0]) in {2, 30} and fields[2].strip():
                    disc_name = fields[2].strip()
            elif line.startswith("TINFO:"):
                fields = robot_fields(line, "TINFO:")
                if len(fields) < 4:
                    continue
                title_id = int(fields[0])
                attribute = int(fields[1])
                value = fields[3].strip()
                title = title_map.setdefault(title_id, TitleInfo(id=title_id))
                if attribute == 2:
                    title.name = value
                elif attribute == 8:
                    title.chapters = int(value) if value.isdigit() else 0
                elif attribute == 9:
                    title.duration = value
                    title.seconds = duration_seconds(value)
                elif attribute == 10:
                    title.size = value
                elif attribute == 11:
                    title.bytes = int(value) if value.isdigit() else 0
                elif attribute == 27:
                    title.filename = value
        except (ValueError, csv.Error, IndexError):
            continue

    titles = sorted(title_map.values(), key=lambda title: title.id)
    titles = [title for title in titles if title.seconds > 0 or title.bytes > 0]
    if not titles:
        return None

    movie = max(titles, key=lambda title: (title.seconds, title.bytes))
    return DiscInfo(
        name=disc_name,
        titles=titles,
        movie_default=[movie.id],
        show_default=episode_candidates(titles),
    )


def safe_component(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} is required.")
    cleaned = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise ValidationError(f"Enter a valid {field_name.lower()}.")
    if len(cleaned) > 120:
        raise ValidationError(f"{field_name} must be 120 characters or fewer.")
    return cleaned


def int_field(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a number.") from exc
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{field_name} must be between {minimum} and {maximum}.")
    return parsed


def build_rip_plan(payload: dict[str, Any], disc: DiscInfo) -> dict[str, Any]:
    media_type = payload.get("media_type")
    if media_type not in {"movie", "show"}:
        raise ValidationError("Choose Movie or TV Show.")

    name = safe_component(payload.get("name"), "Title")
    valid_title_ids = {title.id for title in disc.titles}
    requested_ids = payload.get("title_ids")
    if requested_ids is None:
        requested_ids = disc.movie_default if media_type == "movie" else disc.show_default
    if not isinstance(requested_ids, list) or not requested_ids:
        raise ValidationError("Select at least one disc title.")
    try:
        title_ids = sorted({int(title_id) for title_id in requested_ids})
    except (TypeError, ValueError) as exc:
        raise ValidationError("The selected disc titles are invalid.") from exc
    if not set(title_ids).issubset(valid_title_ids):
        raise ValidationError("One or more selected titles are no longer available.")

    if media_type == "movie":
        if len(title_ids) != 1:
            raise ValidationError("A movie rip must contain exactly one title.")
        year_value = str(payload.get("year", "")).strip()
        year = None
        if year_value:
            year = int_field(year_value, "Year", 1888, 2100)
        library_name = f"{name} ({year})" if year else name
        destination = MEDIA_ROOT / "Movies" / library_name
        filenames = [f"{library_name}.mkv"]
        season = None
        starting_episode = None
    else:
        season = int_field(payload.get("season", 1), "Season", 0, 99)
        starting_episode = int_field(payload.get("starting_episode", 1), "Starting episode", 0, 999)
        destination = MEDIA_ROOT / "Shows" / name / f"Season {season:02d}"
        filenames = [
            f"{name} - S{season:02d}E{starting_episode + index:02d}.mkv"
            for index in range(len(title_ids))
        ]

    destination = destination.resolve()
    if MEDIA_ROOT not in destination.parents:
        raise ValidationError("The destination must remain inside the media directory.")

    conflicts = [str(destination / filename) for filename in filenames if (destination / filename).exists()]
    if conflicts:
        raise ValidationError(f"A destination file already exists: {conflicts[0]}")

    return {
        "media_type": media_type,
        "name": name,
        "year": year if media_type == "movie" else None,
        "season": season,
        "starting_episode": starting_episode,
        "title_ids": title_ids,
        "destination": destination,
        "filenames": filenames,
    }


class DiscController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.phase = "idle"
        self.message = "Insert a disc, then scan it."
        self.progress = 0
        self.current_action = ""
        self.disc: DiscInfo | None = None
        self.job: dict[str, Any] | None = None
        self.process: subprocess.Popen[str] | None = None
        self.cancel_event = threading.Event()
        self.logs: deque[dict[str, str]] = deque(maxlen=MAX_LOG_LINES)
        self.last_error = ""
        self.add_log("Disc Ingest is ready.")

    def add_log(self, message: str) -> None:
        clean = re.sub(r"\s+", " ", message).strip()
        if not clean:
            return
        with self.lock:
            self.logs.append(
                {"time": datetime.now().astimezone().strftime("%H:%M:%S"), "message": clean[:500]}
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "phase": self.phase,
                "message": self.message,
                "progress": self.progress,
                "current_action": self.current_action,
                "disc": asdict(self.disc) if self.disc else None,
                "job": self.job,
                "last_error": self.last_error,
                "logs": list(self.logs)[-80:],
                "media_root": str(MEDIA_ROOT),
                "drive": OPTICAL_BLOCK_DEVICE,
            }

    def _command_environment(self, home: Path | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(home or CONFIG_DIR)
        if Path(MAKEMKV_WRAPPER).exists():
            environment["LD_PRELOAD"] = MAKEMKV_WRAPPER
        return environment

    def _run_command(
        self,
        args: list[str],
        line_handler: Callable[[str], None],
        home: Path | None = None,
    ) -> int:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=self._command_environment(home),
        )
        with self.lock:
            self.process = process
        assert process.stdout is not None
        try:
            for line in process.stdout:
                line_handler(line.rstrip("\r\n"))
                if self.cancel_event.is_set() and process.poll() is None:
                    process.terminate()
            return process.wait()
        finally:
            with self.lock:
                if self.process is process:
                    self.process = None

    def start_scan(self) -> None:
        with self.lock:
            if self.phase in {"scanning", "ripping"}:
                raise ValidationError("The drive is already busy.")
            self.phase = "scanning"
            self.message = "Reading disc structure…"
            self.progress = 0
            self.current_action = "Probing the optical drive"
            self.disc = None
            self.last_error = ""
            self.cancel_event.clear()
        self.add_log("Scanning the optical drive.")
        threading.Thread(target=self._scan_worker, name="disc-scan", daemon=True).start()

    def _scan_worker(self) -> None:
        output_lines: list[str] = []

        def capture(line: str) -> None:
            output_lines.append(line)
            if line.startswith("MSG:"):
                try:
                    fields = robot_fields(line, "MSG:")
                    if len(fields) >= 4:
                        self.add_log(fields[3])
                except (csv.Error, IndexError):
                    pass

        try:
            # MakeMKV may clear an expired key while scanning. Use a disposable
            # copy of its settings so a read-only probe cannot damage config.
            with tempfile.TemporaryDirectory(prefix="disc-ingest-scan-") as temp_home_value:
                temp_home = Path(temp_home_value)
                settings = CONFIG_DIR / "settings.conf"
                if settings.is_file():
                    shutil.copy2(settings, temp_home / "settings.conf")
                (temp_home / ".MakeMKV").symlink_to(temp_home, target_is_directory=True)
                result = self._run_command(
                    [MAKEMKV_BIN, "-r", "--cache=1", "--minlength=0", "info", "disc:0"],
                    capture,
                    home=temp_home,
                )
            disc = parse_disc_info("\n".join(output_lines))
            with self.lock:
                if self.cancel_event.is_set():
                    self.phase = "cancelled"
                    self.message = "Scan cancelled."
                elif disc:
                    self.disc = disc
                    self.phase = "ready"
                    self.message = f"{disc.name} is ready."
                    self.current_action = f"{len(disc.titles)} title(s) found"
                    self.progress = 0
                    self.add_log(f"Disc ready: {disc.name}; {len(disc.titles)} title(s) found.")
                else:
                    self.phase = "no_disc"
                    self.message = "No readable video disc found."
                    self.current_action = "Insert a disc and scan again"
                    self.last_error = "" if result == 0 else "MakeMKV could not open the disc."
                    self.add_log("No readable video disc was found.")
        except FileNotFoundError:
            self._fail("MakeMKV is not installed in this image.")
        except Exception as exc:  # defensive boundary around hardware interaction
            self._fail(f"Disc scan failed: {exc}")

    def start_rip(self, payload: dict[str, Any]) -> None:
        with self.lock:
            if self.phase != "ready" or not self.disc:
                raise ValidationError("Scan a readable disc before starting a rip.")
            plan = build_rip_plan(payload, self.disc)
            self.phase = "ripping"
            self.message = "Ripping selected titles…"
            self.progress = 0
            self.current_action = "Preparing the destination"
            self.last_error = ""
            self.cancel_event.clear()
            self.job = {
                "id": uuid.uuid4().hex[:12],
                "media_type": plan["media_type"],
                "name": plan["name"],
                "destination": str(plan["destination"]),
                "title_ids": plan["title_ids"],
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        self.add_log(f"Starting {plan['media_type']} rip to {plan['destination']}.")
        threading.Thread(target=self._rip_worker, args=(plan,), name="disc-rip", daemon=True).start()

    def _rip_worker(self, plan: dict[str, Any]) -> None:
        job_id = self.job["id"] if self.job else uuid.uuid4().hex[:12]
        stage = STAGING_ROOT / job_id
        stage.mkdir(parents=True, exist_ok=False)
        completed: list[Path] = []
        total_titles = len(plan["title_ids"])

        try:
            for index, (title_id, filename) in enumerate(zip(plan["title_ids"], plan["filenames"])):
                if self.cancel_event.is_set():
                    raise InterruptedError("Rip cancelled.")

                before = {path.resolve() for path in stage.rglob("*.mkv")}
                with self.lock:
                    self.current_action = f"Ripping title {title_id} ({index + 1} of {total_titles})"
                self.add_log(self.current_action)

                def handle(line: str) -> None:
                    if line.startswith("PRGV:"):
                        try:
                            fields = robot_fields(line, "PRGV:")
                            local = (int(fields[1]) / max(int(fields[2]), 1)) * 100
                            with self.lock:
                                self.progress = min(99, round(((index + local / 100) / total_titles) * 100))
                        except (ValueError, IndexError, csv.Error):
                            pass
                    elif line.startswith(("PRGC:", "PRGT:")):
                        try:
                            prefix = line[:5]
                            fields = robot_fields(line, prefix)
                            if len(fields) >= 3:
                                with self.lock:
                                    self.current_action = fields[2]
                        except (IndexError, csv.Error):
                            pass
                    elif line.startswith("MSG:"):
                        try:
                            fields = robot_fields(line, "MSG:")
                            if len(fields) >= 4:
                                self.add_log(fields[3])
                        except (IndexError, csv.Error):
                            pass

                result = self._run_command(
                    [
                        MAKEMKV_BIN,
                        "-r",
                        "--cache=1",
                        "--progress=-stdout",
                        "mkv",
                        "disc:0",
                        str(title_id),
                        str(stage),
                    ],
                    handle,
                )
                if self.cancel_event.is_set():
                    raise InterruptedError("Rip cancelled.")
                if result != 0:
                    raise RuntimeError(f"MakeMKV stopped with exit code {result} while ripping title {title_id}.")

                new_files = [path for path in stage.rglob("*.mkv") if path.resolve() not in before]
                if len(new_files) != 1:
                    raise RuntimeError(
                        f"Expected one MKV from title {title_id}, but found {len(new_files)} new files."
                    )
                renamed = stage / filename
                new_files[0].rename(renamed)
                completed.append(renamed)
                self.add_log(f"Finished title {title_id} as {filename}.")

            destination: Path = plan["destination"]
            destination.mkdir(parents=True, exist_ok=True)
            for source in completed:
                target = destination / source.name
                if target.exists():
                    raise RuntimeError(f"Destination already exists: {target}")
                shutil.move(str(source), str(target))

            shutil.rmtree(stage, ignore_errors=True)
            with self.lock:
                self.phase = "complete"
                self.progress = 100
                self.message = "Rip complete. Files are ready for Jellyfin."
                self.current_action = str(destination)
            self.add_log(f"Rip complete: {destination}")
            if AUTO_EJECT:
                self._eject(log_failure=True)
        except InterruptedError:
            with self.lock:
                self.phase = "cancelled"
                self.message = "Rip cancelled. Partial files were kept outside the Jellyfin library."
                self.current_action = str(stage)
                self.last_error = ""
            self.add_log(f"Rip cancelled; staging files remain at {stage}.")
        except Exception as exc:  # defensive boundary around hardware and filesystem work
            self._fail(f"Rip failed: {exc}", staging=stage)

    def cancel(self) -> None:
        with self.lock:
            if self.phase not in {"scanning", "ripping"}:
                raise ValidationError("There is no active operation to cancel.")
            self.cancel_event.set()
            process = self.process
            self.current_action = "Stopping safely…"
        self.add_log("Cancellation requested.")
        if process and process.poll() is None:
            process.send_signal(signal.SIGTERM)

    def eject(self) -> None:
        with self.lock:
            if self.phase in {"scanning", "ripping"}:
                raise ValidationError("Wait for the active operation to finish before ejecting.")
        self._eject(log_failure=False)

    def _eject(self, log_failure: bool) -> None:
        try:
            result = subprocess.run(
                [EJECT_BIN, "-F", OPTICAL_BLOCK_DEVICE],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                self.add_log("Disc ejected.")
            elif log_failure:
                self.add_log(f"Automatic eject failed: {result.stderr.strip() or 'unknown error'}")
            else:
                raise ValidationError(result.stderr.strip() or "The drive could not eject the disc.")
        except FileNotFoundError as exc:
            if log_failure:
                self.add_log("Automatic eject failed: the eject command is unavailable.")
            else:
                raise ValidationError("The eject command is unavailable.") from exc
        except subprocess.TimeoutExpired as exc:
            if log_failure:
                self.add_log("Automatic eject failed: the drive did not respond.")
            else:
                raise ValidationError("The drive did not respond to the eject request.") from exc

    def _fail(self, message: str, staging: Path | None = None) -> None:
        with self.lock:
            self.phase = "error"
            self.message = message
            self.last_error = message
            self.current_action = f"Staging files: {staging}" if staging else "Check recent activity"
        self.add_log(message)


CONTROLLER = DiscController()


MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class AppHandler(BaseHTTPRequestHandler):
    server_version = "DiscIngest/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"web: {self.address_string()} - {fmt % args}", flush=True)

    def _authorized(self) -> bool:
        if not WEB_USERNAME:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, WEB_USERNAME) and hmac.compare_digest(password, WEB_PASSWORD)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _begin(self, status: int, content_type: str, content_length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self._security_headers()
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._begin(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Disc Ingest", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()
        return False

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == self.headers.get("Host", "")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("Invalid request length.") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValidationError("The request body is missing or too large.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("The request body is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValidationError("The request body must be an object.")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(HTTPStatus.OK, CONTROLLER.snapshot())
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if not self._same_origin():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Cross-origin requests are not allowed."})
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/scan":
                CONTROLLER.start_scan()
            elif path == "/api/rip":
                CONTROLLER.start_rip(self._read_json())
            elif path == "/api/cancel":
                CONTROLLER.cancel()
            elif path == "/api/eject":
                CONTROLLER.eject()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            self._json(HTTPStatus.ACCEPTED, CONTROLLER.snapshot())
        except ValidationError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc), "state": CONTROLLER.snapshot()})
        except Exception as exc:  # prevent internal details from leaking through the API
            CONTROLLER.add_log(f"Request failed: {exc}")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The request could not be completed."})

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in candidate.parents or not candidate.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        body = candidate.read_bytes()
        self._begin(HTTPStatus.OK, MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream"), len(body))
        self.wfile.write(body)


def main() -> None:
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("APP_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AppHandler)
    print(f"Disc Ingest listening on 0.0.0.0:{port}; media root: {MEDIA_ROOT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
