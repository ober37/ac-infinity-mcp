import logging
import threading
import time
from datetime import UTC, datetime

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ac_infinity_mcp.controller import ControllerType, build_write_payload, detect_controller_type
from ac_infinity_mcp.schema import (
    ACInfinityAdvanceConflictError,
    ACInfinityAPIError,
    ACInfinityAuthError,
    ACInfinityDeviceError,
)

logger = logging.getLogger(__name__)

_SENSOR_TYPE_LABELS: dict[int, str] = {
    10: "soil_moisture",
    11: "co2",
    12: "light",
    13: "ph",
    14: "ec_us_cm",
    15: "ec_ms_cm",
    16: "tds_ppm",
    17: "tds_ppt",
    18: "water_temp_c",
    19: "water_temp_f",
    20: "water_level",
}


class ACInfinityClient:
    """Client for AC Infinity cloud API"""

    BASE_URL = "http://www.acinfinityserver.com/api"
    LOGIN_ENDPOINT = f"{BASE_URL}/user/appUserLogin"
    DEVICES_ENDPOINT = f"{BASE_URL}/user/devInfoListAll"
    HISTORY_ENDPOINT = f"{BASE_URL}/log/dataPage"
    MODE_SETTINGS_ENDPOINT = f"{BASE_URL}/dev/getdevModeSettingList"
    ADD_DEV_MODE_ENDPOINT = f"{BASE_URL}/dev/addDevMode"
    MODE_AND_SETTING_ENDPOINT = f"{BASE_URL}/dev/modeAndSetting"

    # v2.0 Automation management endpoints. The path prefix embeds the version
    # string as a literal path segment, which is an unusual but confirmed API design.
    V2_BASE_URL = "http://www.acinfinityserver.com"
    V2_GET_GROUPS_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/getGroups"
    V2_ADD_GROUPS_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/addGroups"
    V2_UPDATE_GROUPS_IS_ON_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/updateGroupsIsOn"
    V2_DEL_BY_ID_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/delByid"

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password[:25]  # API silently truncates to 25 chars
        self.token: str | None = None
        # Shared across asyncio.to_thread calls. urllib3 pool is thread-safe;
        # cookie jar thread safety is moot because this API uses header tokens only.
        self.session = requests.Session()
        self._last_write_time: float = 0.0
        self._write_lock = threading.Lock()
        self._auth_lock = threading.Lock()

    def _raise_for_api_code(self, code: int | None, error_msg: str, context: str) -> None:
        """Map API response code to the appropriate exception."""
        if code == 401:
            raise ACInfinityAuthError(f"Token rejected by API (code 401): {error_msg}")
        raise ACInfinityAPIError(f"{context} API error {code}: {error_msg}")

    def _call_with_token_refresh(self, fn, *args, **kwargs):
        """Call fn(); on a 401 ACInfinityAuthError, re-authenticate once and retry.

        Long-running servers can outlive the API's token TTL. Rather than failing
        the call (forcing a server restart), refresh the token transparently.
        """
        token_at_start = self.token
        try:
            return fn(*args, **kwargs)
        except ACInfinityAuthError:
            if not self.token:
                raise  # never authenticated; nothing to refresh
            with self._auth_lock:
                if self.token == token_at_start:
                    logger.info("Token rejected by API — refreshing")
                    if not self.authenticate():
                        raise
            return fn(*args, **kwargs)

    def _enforce_write_rate_limit(self) -> None:
        """Enforce 1.5s minimum between write API calls (returns 403 if exceeded).

        Held under a lock so concurrent writers serialize correctly — without it,
        parallel tool calls can pass the elapsed-time check simultaneously and
        slam the API back-to-back. _last_write_time is updated under the same
        lock so concurrent waiters see the latest completion timestamp as soon
        as the prior write returns.
        """
        with self._write_lock:
            elapsed = time.monotonic() - self._last_write_time
            if elapsed < 1.5:
                time.sleep(1.5 - elapsed)
            # Provisionally mark the start time so concurrent waiters in this
            # method also serialize; the precise completion time is rewritten
            # by _mark_write_completed() once the POST returns.
            self._last_write_time = time.monotonic()

    def _mark_write_completed(self) -> None:
        """Update _last_write_time to reflect the actual write completion.

        Called immediately after the upstream POST returns (success or HTTP
        error). The pre-POST update inside _enforce_write_rate_limit() set
        the timestamp at the *start* of the call; rewriting it here ensures
        the next write's 1.5s gap is measured from the prior call's
        completion, not its start. Without this, an in-flight 500ms POST
        would leave only ~1.0s of gap before the next caller proceeded,
        risking a 403 rate-limit response from the upstream.
        """
        with self._write_lock:
            self._last_write_time = time.monotonic()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _authenticate_inner(self) -> None:
        """Single login attempt; retried by tenacity on transient network errors."""
        # NOTE: API parameter name has intentional typo — 'appPasswordl' with 'l' at end
        data = {
            "appEmail": self.email,
            "appPasswordl": self.password,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "ACController/1.8.2 (com.acinfinity.humiture; build:489; iOS 16.5.1)",
        }

        resp = self.session.post(self.LOGIN_ENDPOINT, data=data, headers=headers, timeout=10)
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            logger.error("AC Infinity login failed: %s", error_msg)
            raise ACInfinityAuthError(f"Authentication failed: {error_msg}")

        self.token = result["data"]["appId"]
        logger.info("AC Infinity authentication successful")

    def authenticate(self) -> bool:
        """Login and get API token.

        Transient network errors (Timeout, ConnectionError) trigger a tenacity
        retry inside _authenticate_inner; only after exhaustion does this method
        fall back to returning False. Returns False on credential failure as well.
        """
        try:
            self._authenticate_inner()
            return True
        except requests.exceptions.Timeout:
            logger.error("AC Infinity authentication timeout (10s) after retries")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error("Failed to connect to AC Infinity after retries: %s", e)
            return False
        except ACInfinityAuthError:
            return False
        except Exception as e:
            logger.error("AC Infinity authentication error: %s", e)
            return False

    def get_devices(self) -> list[dict]:
        """Fetch all connected devices.

        Returns:
            List of raw device dicts from the AC Infinity API.

        Raises:
            ACInfinityAuthError: If not authenticated or refresh fails.
            ACInfinityAPIError: If the API returns a non-200, non-401 code.
            requests.exceptions.Timeout: After tenacity exhausts retries.
            requests.exceptions.ConnectionError: After tenacity exhausts retries.
        """
        return self._call_with_token_refresh(self._get_devices_inner)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_devices_inner(self) -> list[dict]:
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        params = {"userId": self.token}
        headers = {
            "token": self.token,
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
        }

        resp = self.session.post(
            self.DEVICES_ENDPOINT, params=params, headers=headers, timeout=10
        )
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error("Failed to get devices: %s", error_msg)
            self._raise_for_api_code(code, error_msg, "Devices")

        devices = result.get("data", [])
        logger.info("Fetched %d devices", len(devices))
        return devices

    def get_historical_data(
        self,
        dev_id: str,
        start_timestamp: int,
        end_timestamp: int,
        page_size: int = 2000,
    ) -> list[dict]:
        """Fetch historical sensor data (with transparent 401 token refresh)."""
        return self._call_with_token_refresh(
            self._get_historical_data_inner,
            dev_id, start_timestamp, end_timestamp, page_size,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_historical_data_inner(
        self,
        dev_id: str,
        start_timestamp: int,
        end_timestamp: int,
        page_size: int = 2000,
    ) -> list[dict]:
        """Fetch historical sensor data from AC Infinity cloud API.

        Uses POST /api/log/dataPage.  The API ignores the pageNum parameter
        and always returns the first page_size records starting at 'time'.
        To retrieve more records than page_size, we use time-cursor pagination:
        after each fetch the next request's 'time' is set to the last returned
        record's createTime + 1.

        Args:
            dev_id: Device ID (devId field from devInfoListAll — string or int)
            start_timestamp: Unix timestamp (seconds) for start of range
            end_timestamp: Unix timestamp (seconds) for end of range
            page_size: Records per request (default 2000; API caps at ~1257/day)

        Returns:
            List of raw history record dicts.

        Raises:
            ACInfinityAuthError: If not authenticated (token is None).
            ACInfinityAPIError: If any pagination chunk returns a non-200 code.
            requests.exceptions.Timeout: After tenacity exhausts retries.
            requests.exceptions.ConnectionError: After tenacity exhausts retries.
        """
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        all_records: list[dict] = []
        current_start = start_timestamp
        chunk_num = 0

        while True:
            chunk_num += 1
            data = {
                "appId": self.token,
                "devId": dev_id,
                "time": current_start,
                "endTime": end_timestamp,
                "pageNum": 1,       # API ignores pageNum; always 1
                "pageSize": page_size,
            }
            headers = {
                "token": self.token,
                "Host": "www.acinfinityserver.com",
                "User-Agent": "okhttp/3.10.0",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            }

            resp = self.session.post(
                self.HISTORY_ENDPOINT, data=data, headers=headers, timeout=30
            )
            resp.raise_for_status()

            result = resp.json()
            if result.get("code") != 200:
                error_msg = result.get("msg", "Unknown error")
                code = result.get("code")
                logger.error(
                    "History fetch failed (chunk %d): %s", chunk_num, error_msg
                )
                self._raise_for_api_code(code, error_msg, "History")

            rows = result.get("data", {}).get("rows", [])
            if not rows:
                break

            for row in rows:
                create_time = row.get("createTime", 0)
                if start_timestamp <= create_time <= end_timestamp:
                    all_records.append(row)

            if len(rows) < page_size:
                break

            # Advance time cursor past the last record's timestamp
            last_ts = rows[-1].get("createTime", 0)
            if last_ts <= current_start or last_ts >= end_timestamp:
                break
            current_start = last_ts + 1

        logger.info(
            "Fetched %d history records for devId=%s in %d chunk(s)",
            len(all_records),
            dev_id,
            chunk_num,
        )
        return all_records

    def get_mode_settings(self, dev_id: str | int, port: int) -> dict:
        """Fetch current mode settings (with transparent 401 token refresh)."""
        return self._call_with_token_refresh(
            self._get_mode_settings_inner, dev_id, port,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_mode_settings_inner(self, dev_id: str | int, port: int) -> dict:
        """Fetch current mode settings for one port on a device.

        Required for read-before-write (Quirk 13). The port parameter is mandatory
        (Quirk 16) — the endpoint returns a single dict for that port, not a list.

        Args:
            dev_id: Numeric device ID (devId field from devInfoListAll — Quirk 7).
            port: 1-based port number.

        Returns:
            142-field dict from the API response data. Nested fields (devSetting,
            fieldSet, ipcSetting) are present but excluded by build_write_payload.

        Raises:
            ACInfinityAuthError: If not authenticated or token rejected (code 401).
            ACInfinityAPIError: If the API returns a non-200 code.
            requests.exceptions.Timeout: After tenacity exhausts retries.
            requests.exceptions.ConnectionError: After tenacity exhausts retries.
        """
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        headers = {
            "token": self.token,
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        data = {"devId": dev_id, "port": port, "appId": self.token}

        resp = self.session.post(
            self.MODE_SETTINGS_ENDPOINT, data=data, headers=headers, timeout=10
        )
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to get mode settings (devId=%s port=%s): %s", dev_id, port, error_msg
            )
            self._raise_for_api_code(code, error_msg, "Mode settings")

        settings = result.get("data") or {}
        logger.debug("Fetched mode settings for devId=%s port=%s", dev_id, port)
        return settings

    def set_port_mode(
        self,
        device_data: dict,
        port: int,
        updates: dict,
        dry_run: bool = True,
        require_variable_speed: bool = False,
    ) -> dict:
        """Write port mode settings (with transparent 401 token refresh)."""
        return self._call_with_token_refresh(
            self._set_port_mode_inner,
            device_data, port, updates, dry_run, require_variable_speed,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        # ConnectionError fires before the request reaches the server, so retry is
        # safe — the write hasn't been applied. Timeout is intentionally excluded:
        # a read timeout can mean the server already processed the write and the
        # response was lost, so retrying would risk double-applying state.
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _set_port_mode_inner(
        self,
        device_data: dict,
        port: int,
        updates: dict,
        dry_run: bool = True,
        require_variable_speed: bool = False,
    ) -> dict:
        """Write port mode settings using read-before-write.

        Reads current settings, merges updates, and optionally POSTs to addDevMode.
        Both legacy and AI+ controllers use the same read-before-write pattern since
        getdevModeSettingList returns the same 142-field structure for both.

        Args:
            device_data: Full device dict from get_devices() — used for controller
                type detection and devId lookup.
            port: 1-based port number.
            updates: Fields to change, e.g. {"onSpead": 5}.
            dry_run: If True (default), build and return the payload without sending.
            require_variable_speed: If True, raise ACInfinityDeviceError when the port's
                loadType indicates on/off hardware (loadType=4 or 128). Pass True from
                set_port_speed; leave False for set_port_on/set_port_off.

        Returns:
            Dict with keys:
                "payload": the complete dict that would be / was sent
                "dry_run": bool
                "controller_type": "legacy" or "new_framework"
                "sent": bool (True only when dry_run=False and HTTP succeeded)
                "ai_plus_write_unsupported": bool (True when AI+ live write attempted)

        Raises:
            ACInfinityAuthError: If not authenticated.
            ACInfinityAPIError: If the API returns a non-200 code (only when dry_run=False).
            ACInfinityDeviceError: If devId is missing from device_data, if the port is in
                smart automation mode (modeType=15), or if require_variable_speed=True and
                the port's loadType indicates on/off hardware.
        """
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        dev_id = device_data.get("devId")
        if not dev_id:
            raise ACInfinityDeviceError("device_data missing devId field")

        controller_type = detect_controller_type(device_data)
        current_settings = self.get_mode_settings(dev_id, port)

        # Guard: smart automation mode cannot be overridden via the write API (returns 999999)
        # Only fire when isOpenAutomation != 0 (absent field defaults to 1 = assume active).
        mode_type = current_settings.get("modeType")
        if mode_type == 15 and current_settings.get("isOpenAutomation", 1) != 0:
            raise ACInfinityAdvanceConflictError(
                f"Port {port} on device {dev_id} is in smart automation mode (modeType=15) — "
                "cannot override manually."
            )

        # Guard: on/off hardware (loadType=4 or 128) rejects speed writes with 999999.
        # Only enforced when require_variable_speed=True (i.e. called from set_port_speed).
        load_type = current_settings.get("loadType", 0)
        if require_variable_speed and load_type in (4, 128):
            raise ACInfinityDeviceError(
                f"Port {port} is an on/off device (loadType={load_type}) — "
                "use set_port_on or set_port_off instead of set_port_speed."
            )

        payload = build_write_payload(current_settings, updates, controller_type)

        result: dict = {
            "payload": payload,
            "dry_run": dry_run,
            "controller_type": controller_type.value,
            "sent": False,
        }

        if dry_run:
            logger.debug(
                "Dry run — payload built for devId=%s port=%s (%d fields)",
                dev_id, port, len(payload),
            )
            return result

        # AI+ live write path is not yet implemented — addDevMode returns 100001 for devType=22
        # and no alternative endpoint has been identified. dry_run=True is fully supported.
        if controller_type == ControllerType.NEW_FRAMEWORK:
            logger.warning(
                "AI+ live write attempted for devId=%s port=%s — not yet supported", dev_id, port
            )
            result["ai_plus_write_unsupported"] = True
            return result

        headers = {
            "token": self.token,
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }

        # Retry loop: 403 "Data saving failed" = rate limit; back off and retry.
        # Other error codes fail immediately (auth, field validation, etc.).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            self._enforce_write_rate_limit()
            try:
                resp = self.session.post(
                    self.ADD_DEV_MODE_ENDPOINT, data=payload, headers=headers, timeout=10
                )
            finally:
                # Anchor the next rate-limit gap from the POST's completion
                # (or error) rather than its start (P1-F015).
                self._mark_write_completed()
            resp.raise_for_status()

            write_result = resp.json()
            if write_result.get("code") == 200:
                break

            error_msg = write_result.get("msg", "Unknown error")
            code = write_result.get("code")

            if code == 403 and "saving failed" in error_msg.lower() and attempt < max_attempts:
                logger.warning(
                    "Write rate-limit hit for devId=%s port=%s (attempt %d/%d), backing off 3s",
                    dev_id, port, attempt, max_attempts,
                )
                time.sleep(3)
                continue

            logger.error("Write failed for devId=%s port=%s: %s", dev_id, port, error_msg)
            self._raise_for_api_code(code, error_msg, "Write")
        else:  # pragma: no cover — defensive; current control flow always break/raise first
            # Defensive guard (P1-F017): the loop above must either break on
            # a 200 response or raise via _raise_for_api_code. If a future
            # refactor breaks that invariant (e.g. reorders the retry guard),
            # this else clause prevents the function from silently falling
            # through and reporting sent=True for a write that never succeeded.
            raise ACInfinityAPIError(
                f"Write loop exited without success or explicit failure for "
                f"devId={dev_id} port={port} — internal invariant violated"
            )

        logger.info("Wrote mode settings for devId=%s port=%s", dev_id, port)
        result["sent"] = True
        return result

    # ============ v2.0 Automation Management Methods ============

    def _v2_headers(self) -> dict[str, str]:
        """Build the additional headers required for v2.0 API endpoints.

        The v2.0 API validates several app-identity headers that the legacy API
        does not require. The `sign` header is omitted — the server accepts requests
        without it (confirmed in Phase 17 network capture).
        """
        return {
            "token": self.token or "",
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "version": "555",
            "phoneType": "1",
            "devType": "18",
            "appVersion": "2.0.4",
            "requestId": str(int(time.time() * 1000)),
            "languageType": "en-US",
            "languageVersion": "idongle_pro_3",
        }

    def get_advance_automations(self, dev_id: str) -> list[dict]:
        """Fetch all automation group entries for a device (with transparent 401 refresh).

        Returns a flat list of raw automation entries from the getGroups endpoint.
        One user-visible automation may map to multiple entries with different advId
        values (one per port-speed group) but the same advName.

        Args:
            dev_id: Numeric device ID string (devId field from devInfoListAll).

        Returns:
            List of raw automation entry dicts. Empty list if no automations.

        Raises:
            ACInfinityAuthError: If not authenticated or token refresh fails.
            ACInfinityAPIError: If the API returns a non-200, non-401 code.
        """
        return self._call_with_token_refresh(self._get_advance_automations_inner, dev_id)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_advance_automations_inner(self, dev_id: str) -> list[dict]:
        """POST /api/version=2.0/dev/getGroups — returns data array."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        resp = self.session.post(
            self.V2_GET_GROUPS_ENDPOINT,
            data={"devId": dev_id},
            headers=self._v2_headers(),
            timeout=10,
        )
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error("Failed to get advance automations (devId=%s): %s", dev_id, error_msg)
            self._raise_for_api_code(code, error_msg, "GetGroups")

        data = result.get("data") or []
        logger.info("Fetched %d automation entries for devId=%s", len(data), dev_id)
        return data

    def enable_advance_automation(self, dev_id: str, adv_id: int) -> dict:
        """Toggle automation to enabled state (with transparent 401 refresh).

        IMPORTANT: updateGroupsIsOn TOGGLES the current isOn state server-side.
        The caller must verify the current state is disabled before calling this
        method, to ensure the toggle results in enabled state.

        Args:
            dev_id: Numeric device ID string.
            adv_id: Automation entry ID to toggle.

        Returns:
            API response dict.
        """
        return self._call_with_token_refresh(
            self._enable_advance_automation_inner, dev_id, adv_id
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        # ConnectionError fires before the request reaches the server — safe to retry.
        # Timeout excluded: server may have processed the write; retrying risks double-apply.
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _enable_advance_automation_inner(self, dev_id: str, adv_id: int) -> dict:
        """POST /api/version=2.0/dev/updateGroupsIsOn — toggles isOn state."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_UPDATE_GROUPS_IS_ON_ENDPOINT,
                data={"advId": adv_id, "isDel": 0, "isflag": 1},
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to enable automation advId=%s (devId=%s): %s", adv_id, dev_id, error_msg
            )
            self._raise_for_api_code(code, error_msg, "EnableAutomation")

        logger.info("Toggled automation advId=%s to enabled (devId=%s)", adv_id, dev_id)
        return result

    def disable_advance_automation(self, dev_id: str, adv_id: int) -> dict:
        """Toggle automation to disabled state (with transparent 401 refresh).

        IMPORTANT: updateGroupsIsOn TOGGLES the current isOn state server-side.
        The caller must verify the current state is enabled before calling this
        method, to ensure the toggle results in disabled state.

        Args:
            dev_id: Numeric device ID string.
            adv_id: Automation entry ID to toggle.

        Returns:
            API response dict.
        """
        return self._call_with_token_refresh(
            self._disable_advance_automation_inner, dev_id, adv_id
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _disable_advance_automation_inner(self, dev_id: str, adv_id: int) -> dict:
        """POST /api/version=2.0/dev/updateGroupsIsOn — toggles isOn state (same body as enable)."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_UPDATE_GROUPS_IS_ON_ENDPOINT,
                data={"advId": adv_id, "isDel": 0, "isflag": 1},
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to disable automation advId=%s (devId=%s): %s", adv_id, dev_id, error_msg
            )
            self._raise_for_api_code(code, error_msg, "DisableAutomation")

        logger.info("Toggled automation advId=%s to disabled (devId=%s)", adv_id, dev_id)
        return result

    def create_advance_automation(self, dev_id: str, payload: dict) -> dict:
        """Create a new advance automation group (with transparent 401 refresh).

        Args:
            dev_id: Numeric device ID string.
            payload: Complete form payload for addGroups. Must include at minimum
                advName, devId, onSpeed. The caller is responsible for constructing
                the full ~50-field payload with safe defaults.

        Returns:
            Created automation object with server-assigned advId.
        """
        return self._call_with_token_refresh(
            self._create_advance_automation_inner, dev_id, payload
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _create_advance_automation_inner(self, dev_id: str, payload: dict) -> dict:
        """POST /api/version=2.0/dev/addGroups — creates automation, returns created object."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        form_data = {**payload, "devId": dev_id}

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_ADD_GROUPS_ENDPOINT,
                data=form_data,
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error("Failed to create automation (devId=%s): %s", dev_id, error_msg)
            self._raise_for_api_code(code, error_msg, "CreateAutomation")

        data = result.get("data") or {}
        logger.info("Created automation for devId=%s, advId=%s", dev_id, data.get("advId"))
        return data

    def delete_advance_automation(self, dev_id: str, adv_id: int) -> dict:
        """Delete an advance automation group entry (with transparent 401 refresh).

        Args:
            dev_id: Numeric device ID string.
            adv_id: Automation entry ID to delete.

        Returns:
            API response dict.
        """
        return self._call_with_token_refresh(
            self._delete_advance_automation_inner, dev_id, adv_id
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _delete_advance_automation_inner(self, dev_id: str, adv_id: int) -> dict:
        """POST /api/version=2.0/dev/delByid — deletes automation entry."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_DEL_BY_ID_ENDPOINT,
                data={"advId": adv_id, "isDel": 1, "isflag": 1},
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to delete automation advId=%s (devId=%s): %s", adv_id, dev_id, error_msg
            )
            self._raise_for_api_code(code, error_msg, "DeleteAutomation")

        logger.info("Deleted automation advId=%s (devId=%s)", adv_id, dev_id)
        return result

    def parse_device_data(self, device_data: dict, role: str | None = None) -> dict:
        """Extract readable values from AC Infinity device response.

        Type errors in the upstream response (a field arriving as a string
        where the parser expects an int, etc.) are converted to a typed
        ACInfinityAPIError so tool-level handlers log the structural issue
        clearly rather than re-raising raw TypeError text to the LLM (P3-F011).
        """
        try:
            info = device_data.get("deviceInfo", {})

            # API returns values * 100 — divide to get actual readings
            temp_c = info.get("temperature", 0) / 100.0
            temp_f = info.get("temperatureF", 0) / 100.0
            humidity = info.get("humidity", 0) / 100.0
            vpd = round(info.get("vpdnums", 0) / 100.0, 2)

            raw_ports = info.get("ports", [])
            ports = [
                {
                    "port": p.get("port"),
                    "name": p.get("portName", f"Port {p.get('port')}"),
                    "speed": p.get("speak", 0),  # 0-10 scale from API
                    "load": p.get("portsLoad", 0),
                }
                for p in raw_ports
            ]

            sensors = info.get("sensors")
            external = []
            if sensors:
                external = [
                    {
                        "sensor_id": f"{s.get('accessPort')}.{s.get('sensorType')}",
                        "sensor_type": s.get("sensorType"),
                        "sensor_type_label": _SENSOR_TYPE_LABELS.get(
                            s.get("sensorType"), "unknown"
                        ),
                        "value": s.get("sensorData", 0) / (s.get("sensorPrecision") or 100),
                    }
                    for s in sensors
                ]

            return {
                "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
                "device_id": device_data.get("devCode"),
                "device_name": device_data.get("devName", "Unknown"),
                "temperature_c": round(temp_c, 1),
                "temperature_f": round(temp_f, 1),
                "humidity": round(humidity, 1),
                "vpd": vpd,
                "ports": ports,
                "external_sensors": external,
            }
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning(
                "Malformed device data for devCode=%s: %s",
                device_data.get("devCode") if isinstance(device_data, dict) else "<non-dict>",
                e,
            )
            raise ACInfinityAPIError(
                "AC Infinity API returned malformed device data"
            ) from e

    def parse_history_record(
        self, record: dict, port_names: dict[int, str] | None = None
    ) -> dict:
        """Parse a historical data record from the AC Infinity API.

        The API encodes port data as bitmask integers rather than a ports array:
        - ``portSpead``: 4 bits (one nibble) per port, LSB = Port 1.
          Values 0-10 are fan/dimmer speeds; 0xF (15) means ON for
          on/off devices (lights, heaters, humidifiers, heat pads).
        - ``portStatus``: 1 bit per port, LSB = Port 1.  Indicates
          whether the port was actively triggered by automation.

        Args:
            record: Raw historical record from get_historical_data API call
            port_names: Optional mapping of port number -> name from live
                device info.  When provided the names are attached to
                each decoded port entry.

        Returns:
            Dict with parsed timestamp, temperature, humidity, VPD, and port data.

        Raises:
            ACInfinityAPIError: when the upstream record is malformed (wrong
                field types — e.g. portSpead as a string rather than int).
                Defense in depth so a poisoned response cannot surface raw
                TypeError text to the LLM via the tool-level handlers (P3-F011).
        """
        try:
            create_time = record.get("createTime", 0)
            timestamp = (
                datetime.fromtimestamp(int(create_time), UTC).replace(tzinfo=None).isoformat()
                + "Z"
                if create_time
                else None
            )

            # Decode port speeds from portSpead bitmask (4 bits per port). Quirk 6:
            # portStatus is the "automation-triggered" flag, NOT the on/off state.
            # The speed nibble alone is authoritative for on/off — a port can be
            # automation-armed (status bit set) with nibble=0 (idle), which used
            # to be reported as ON, overstating runtime in the activity report.
            port_spead = record.get("portSpead", 0) or 0
            port_status = record.get("portStatus", 0) or 0
            port_count = record.get("devPortCount") or 8

            ports = []
            for i in range(port_count):
                nibble = (port_spead >> (i * 4)) & 0xF
                on = nibble > 0
                automation_triggered = bool((port_status >> i) & 1)
                speed = 1 if nibble == 0xF else nibble  # 0xF = ON for toggle devices
                name = (port_names or {}).get(i + 1, f"Port {i + 1}")
                ports.append({
                    "port": i + 1,
                    "name": name,
                    "speed": speed,
                    "on": on,
                    "automation_triggered": automation_triggered,
                })

            return {
                "timestamp": timestamp,
                "temperature_c": round(record.get("temperature", 0) / 100.0, 1),
                "temperature_f": round(record.get("fTemperature", 0) / 100.0, 1),
                "humidity": round(record.get("humidity", 0) / 100.0, 1),
                "vpd": round(record.get("vpdNums", 0) / 100.0, 2),
                "leaf_temp_c": round(record.get("leafTemp", 0) / 10.0, 1),
                "ports": ports,
            }
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning("Malformed history record: %s", e)
            raise ACInfinityAPIError(
                "AC Infinity API returned malformed history record"
            ) from e
