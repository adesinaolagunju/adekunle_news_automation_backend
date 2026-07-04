import requests


class BufferServiceError(Exception):
    """Base exception for Buffer service failures."""


class BufferAuthenticationError(BufferServiceError):
    """Raised when Buffer rejects or cannot authenticate the token."""


class BufferAPIError(BufferServiceError):
    """Raised when Buffer returns an API-level error."""


class BufferService:
    """Service for interacting with Buffer's publishing API."""

    BASE_URL = "https://api.bufferapp.com/1"

    def __init__(self, access_token, timeout=30):
        if not access_token:
            raise ValueError("Buffer access token is required")

        self.access_token = access_token
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method, endpoint, params=None, data=None):
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        params = params.copy() if params else {}
        data = data.copy() if data else {}

        if method.upper() == "GET":
            params["access_token"] = self.access_token
        else:
            data["access_token"] = self.access_token

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                data=data,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise BufferServiceError("Buffer request timed out") from exc
        except requests.ConnectionError as exc:
            raise BufferServiceError("Could not connect to Buffer") from exc
        except requests.RequestException as exc:
            raise BufferServiceError(f"Buffer request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BufferAPIError(
                f"Buffer returned a non-JSON response with status {response.status_code}"
            ) from exc

        if response.status_code in (401, 403):
            message = self._extract_error_message(payload, "Buffer authentication failed")
            raise BufferAuthenticationError(message)

        if response.status_code >= 400:
            message = self._extract_error_message(
                payload,
                f"Buffer API request failed with status {response.status_code}",
            )
            raise BufferAPIError(message)

        if isinstance(payload, dict) and payload.get("success") is False:
            message = self._extract_error_message(payload, "Buffer API request was unsuccessful")
            raise BufferAPIError(message)

        return payload

    @staticmethod
    def _extract_error_message(payload, default):
        if isinstance(payload, dict):
            for key in ("message", "error", "error_message"):
                value = payload.get(key)
                if value:
                    return str(value)

            errors = payload.get("errors")
            if errors:
                return str(errors)

        return default

    def test_connection(self):
        """Validate the access token and return Buffer user/account data."""
        return self._request("GET", "user.json")

    def get_profiles(self):
        """Return Buffer profiles available to the authenticated account."""
        return self._request("GET", "profiles.json")

    def create_post(self, profile_ids, text, media=None, shorten=True):
        """Create a queued Buffer post for one or more profiles."""
        return self._create_update(
            profile_ids=profile_ids,
            text=text,
            media=media,
            shorten=shorten,
        )

    def schedule_post(self, profile_ids, text, scheduled_at, media=None, shorten=True):
        """Create a Buffer post scheduled for a specific Unix timestamp or datetime string."""
        if not scheduled_at:
            raise ValueError("scheduled_at is required when scheduling a Buffer post")

        return self._create_update(
            profile_ids=profile_ids,
            text=text,
            media=media,
            shorten=shorten,
            scheduled_at=scheduled_at,
        )

    def publish_post(self, profile_ids, text, media=None, shorten=True):
        """Create and publish a Buffer post immediately."""
        return self._create_update(
            profile_ids=profile_ids,
            text=text,
            media=media,
            shorten=shorten,
            now=True,
        )

    def _create_update(
        self,
        profile_ids,
        text,
        media=None,
        shorten=True,
        now=False,
        scheduled_at=None,
    ):
        if not profile_ids:
            raise ValueError("At least one Buffer profile id is required")

        if not text:
            raise ValueError("Post text is required")

        if isinstance(profile_ids, str):
            profile_ids = [profile_ids]

        data = {
            "text": text,
            "shorten": "true" if shorten else "false",
        }

        for index, profile_id in enumerate(profile_ids):
            data[f"profile_ids[{index}]"] = profile_id

        if now:
            data["now"] = "true"

        if scheduled_at:
            data["scheduled_at"] = scheduled_at

        if media:
            self._add_media(data, media)

        return self._request("POST", "updates/create.json", data=data)

    @staticmethod
    def _add_media(data, media):
        if not isinstance(media, dict):
            raise ValueError("media must be a dictionary")

        link = media.get("link")
        description = media.get("description")
        title = media.get("title")
        photo = media.get("photo")
        thumbnail = media.get("thumbnail")

        if link:
            data["media[link]"] = link
        if description:
            data["media[description]"] = description
        if title:
            data["media[title]"] = title
        if photo:
            data["media[photo]"] = photo
        if thumbnail:
            data["media[thumbnail]"] = thumbnail
