import logging
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from time import sleep

from aw_client import ActivityWatchClient
from aw_core.log import setup_logging
from aw_core.models import Event

from .config import parse_args
from .exceptions import FatalError
from .lib import get_current_window
from .macos_permissions import background_ensure_permissions

logger = logging.getLogger(__name__)

# run with LOG_LEVEL=DEBUG
log_level = os.environ.get("LOG_LEVEL")
if log_level:
    logger.setLevel(logging.__getattribute__(log_level.upper()))


def kill_process(pid):
    logger.info("Killing process {}".format(pid))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        logger.info("Process {} already dead".format(pid))


def try_compile_title_regex(title):
    try:
        return re.compile(title, re.IGNORECASE)
    except re.error:
        logger.error(f"Invalid regex pattern: {title}")
        exit(1)


def main():
    args = parse_args()

    if sys.platform.startswith("linux") and (
        "DISPLAY" not in os.environ or not os.environ["DISPLAY"]
    ):
        logger.error("===>> DISPLAY environment variable not set on Linux")
        raise Exception("DISPLAY environment variable not set")

    logger.info("===>> Setting up logging system")
    setup_logging(
        name="aw-watcher-window",
        testing=args.testing,
        verbose=args.verbose,
        log_stderr=True,
        log_file=True,
    )
    logger.info(f"===>> Logging setup complete - Testing: {args.testing}, Verbose: {args.verbose}")

    if sys.platform == "darwin":
        logger.info("===>> macOS detected, checking accessibility permissions")
        background_ensure_permissions()
        logger.info("===>> Accessibility permission check completed")

    logger.info("===>> Creating ActivityWatch client")
    client = ActivityWatchClient(
        "aw-watcher-window", host=args.host, port=args.port, testing=args.testing
    )
    logger.info(f"===>> Client created: {client.client_name}@{client.client_hostname}")

    bucket_id = f"{client.client_name}_{client.client_hostname}"
    event_type = "currentwindow"

    logger.info("===>> Creating bucket for event tracking")
    client.create_bucket(bucket_id, event_type, queued=True)
    logger.info("===>> Bucket created successfully")

    logger.info("===>> aw-watcher-window started successfully")
    logger.info(f"===>> Bucket ID: {bucket_id}")
    logger.info(f"===>> Event type: {event_type}")
    logger.info(f"===>> Strategy: {args.strategy}")
    client.wait_for_start()
    logger.info("===>> aw-server is ready, starting event tracking")

    with client:
        if sys.platform == "darwin" and args.strategy == "swift":
            logger.info("===>> Using swift strategy, calling out to swift binary")
            binpath = os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "aw-watcher-window-macos"
            )
            logger.info(f"===>> Swift binary path: {binpath}")
            logger.info(f"===>> Server address: {client.server_address}")
            logger.info(f"===>> Bucket ID: {bucket_id}")
            logger.info(f"===>> Client hostname: {client.client_hostname}")
            logger.info(f"===>> Client name: {client.client_name}")

            try:
                swift_cmd = [
                    binpath,
                    client.server_address,
                    bucket_id,
                    client.client_hostname,
                    client.client_name,
                ]
                logger.info(f"===>> Swift command: {swift_cmd}")
                
                p = subprocess.Popen(swift_cmd)
                logger.info(f"===>> Swift process started with PID: {p.pid}")
                
                # terminate swift process when this process dies
                signal.signal(signal.SIGTERM, lambda *_: kill_process(p.pid))
                return_code = p.wait()
                logger.info(f"===>> Swift process finished with return code: {return_code}")
                
                # Monitor Swift binary crash frequency for debugging
                if return_code == -6:  # SIGABRT
                    logger.error(f"===>> CRASH DETECTED: Swift binary crashed with SIGABRT (return code -6)")
                    logger.error(f"===>> This indicates memory access violation or assertion failure")
                    logger.error(f"===>> Crash frequency monitoring: This is crash #{return_code} in current session")
                    logger.error(f"===>> Please check macOS accessibility permissions and Swift binary integrity")
                    logger.error(f"===>> Consider falling back to python strategy if crashes persist")
                elif return_code != 0:
                    logger.warning(f"===>> Swift process exited with non-zero code: {return_code}")
                    logger.warning(f"===>> This may indicate a configuration or permission issue")
            except KeyboardInterrupt:
                logger.info("===>> KeyboardInterrupt received")
                print("KeyboardInterrupt")
                kill_process(p.pid)
            except Exception as e:
                logger.error(f"===>> Failed to start swift process: {e}")
                raise
        else:
            logger.info(f"===>> Using {args.strategy} strategy for event tracking")
            heartbeat_loop(
                client,
                bucket_id,
                poll_time=args.poll_time,
                strategy=args.strategy,
                exclude_title=args.exclude_title,
                exclude_titles=[
                    try_compile_title_regex(title)
                    for title in args.exclude_titles
                    if title is not None
                ],
            )


def heartbeat_loop(
    client, bucket_id, poll_time, strategy, exclude_title=False, exclude_titles=[]
):
    consecutive_errors = 0
    logger.info(f"===>> Starting heartbeat loop with strategy: {strategy}")
    logger.info(f"===>> Poll time: {poll_time}s, Bucket: {bucket_id}")
    
    while True:
        if os.getppid() == 1:
            logger.info("===>> window-watcher stopped because parent process died")
            break

        current_window = None
        try:
            current_window = get_current_window(strategy)
            logger.debug(f"===>> Current window: {current_window}")
            consecutive_errors = 0
        except (FatalError, OSError) as e:
            consecutive_errors += 1
            try:
                logger.error(f"===>> FATAL ERROR #{consecutive_errors}: {type(e).__name__}: {e}")
                logger.exception("===>> Fatal error details")
            except OSError:
                pass
            if consecutive_errors >= 3:
                logger.error("===>> Too many consecutive fatal errors, exiting")
                break
            logger.warning(f"===>> Waiting {poll_time}s before retry")
            sleep(poll_time)
            continue
        except Exception as e:
            consecutive_errors += 1
            try:
                logger.error(f"===>> ERROR #{consecutive_errors}: {type(e).__name__}: {e}")
                logger.exception("===>> Exception details")
            except OSError:
                break
            if consecutive_errors >= 5:
                logger.error("===>> Too many consecutive errors, exiting")
                break

        if current_window is None:
            logger.debug("===>> Unable to fetch window, trying again on next poll")
        else:
            for pattern in exclude_titles:
                if pattern.search(current_window["title"]):
                    current_window["title"] = "excluded"
                    logger.debug(f"===>> Window excluded by pattern: {pattern}")

            if exclude_title:
                current_window["title"] = "excluded"
                logger.debug("===>> Window excluded due to exclude_title flag")

            now = datetime.now(timezone.utc)
            current_window_event = Event(timestamp=now, data=current_window)

            logger.debug(f"===>> Sending heartbeat for: {current_window['app']} - {current_window['title']}")
            # Set pulsetime to 1 second more than the poll_time
            # This since the loop takes more time than poll_time
            # due to sleep(poll_time).
            client.heartbeat(
                bucket_id, current_window_event, pulsetime=poll_time + 1.0, queued=True
            )
            logger.debug("===>> Heartbeat sent successfully")

        sleep(poll_time)
