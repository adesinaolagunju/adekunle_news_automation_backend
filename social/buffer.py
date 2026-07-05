"""
social/buffer.py

Client for Buffer's current GraphQL API (https://api.buffer.com).

Buffer retired new developer registrations for the legacy REST API
(api.bufferapp.com/1, OAuth2 client_id/secret flow). Personal API keys
generated from Settings -> API (publish.buffer.com/settings/api) only work
against this new GraphQL endpoint, authenticated with a Bearer token.

Notes / caveats (as of this writing, the API is still in public beta):
- "Profiles" are now called "channels".
- Posts belong to a single channelId (no more posting to several
  profile_ids in one call) - loop and call create_post per channel.
- There is no documented "publish immediately" mode. The two supported
  modes are addToQueue (next open slot) and customScheduled (specific
  dueAt). publish_now() below approximates "now" with customScheduled,
  but confirm against the API Explorer if exact timing matters.
- Media is passed via `assets[{image:{url:...}}]` per the AssetInput schema
- delete_post()/edit_post() field shapes aren't fully confirmed here -
  verify DeletePostInput / EditPostInput in the API Explorer
  (https://developers.buffer.com) before relying on them in production.
"""

import requests


class BufferServiceError(Exception):
    """Base exception for Buffer service failures."""


class BufferAuthenticationError(BufferServiceError):
    """Raised when Buffer rejects or cannot authenticate the API key."""


class BufferAPIError(BufferServiceError):
    """Raised when Buffer returns a GraphQL-level error."""


class BufferService:
    """Service for interacting with Buffer's GraphQL API."""

    BASE_URL = "https://api.buffer.com"

    def __init__(self, api_key, timeout=30):
        if not api_key:
            raise ValueError("Buffer API key is required")

        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })

    def _request(self, query, variables=None):
        payload = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        try:
            response = self.session.post(
                self.BASE_URL,
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise BufferServiceError("Buffer request timed out") from exc
        except requests.ConnectionError as exc:
            raise BufferServiceError("Could not connect to Buffer") from exc
        except requests.RequestException as exc:
            raise BufferServiceError(f"Buffer request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise BufferAuthenticationError(
                "Buffer rejected the API key (401/403). Regenerate it at "
                "Settings -> API if it may have expired."
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise BufferAPIError(
                f"Buffer returned a non-JSON response with status {response.status_code}"
            ) from exc

        if response.status_code >= 400:
            message = self._extract_top_level_error(body) or (
                f"Buffer API request failed with status {response.status_code}"
            )
            raise BufferAPIError(message)

        errors = body.get("errors")
        if errors:
            message = self._extract_top_level_error(body)
            codes = {
                (err.get("extensions") or {}).get("code")
                for err in errors
                if isinstance(err, dict)
            }
            if codes & {"UNAUTHORIZED", "FORBIDDEN"}:
                raise BufferAuthenticationError(message)
            raise BufferAPIError(message)

        return body.get("data") or {}

    @staticmethod
    def _extract_top_level_error(body):
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            parts = []
            for err in errors:
                if not isinstance(err, dict):
                    continue
                msg = err.get("message")
                if msg:
                    parts.append(msg)
                ext = err.get("extensions")
                if isinstance(ext, dict):
                    code = ext.get("code")
                    if code:
                        parts.append(f"[code={code}]")
                loc = err.get("locations")
                if isinstance(loc, list) and loc:
                    parts.append(f"at line {loc[0].get('line', '?')}")
            if parts:
                return "; ".join(parts)
        return None

    @staticmethod
    def _raise_if_mutation_error(result, action_key, default_message):
        payload = result.get(action_key) if result else None
        if payload is None:
            raise BufferAPIError(
                f"{default_message}. Full response: {result}"
            )
        if "message" in payload and "post" not in payload:
            raise BufferAPIError(payload["message"])
        return payload

    @staticmethod
    def _service_to_metadata_key(service):
        """Map a channel service value to its PostInputMetaData field name."""
        mapping = {
            'instagram': 'instagram',
            'facebook': 'facebook',
            'twitter': 'twitter',
            'tiktok': 'tiktok',
            'linkedin': 'linkedin',
            'pinterest': 'pinterest',
            'youtube': 'youtube',
            'google_my_business': 'googleBusiness',
            'mastodon': 'mastodon',
            'threads': 'threads',
            'bluesky': 'bluesky',
        }
        return mapping.get(service)

    @staticmethod
    def _add_platform_metadata(post_input, service):
        """Mutate *post_input* with platform-specific metadata for *service*.

        Called after the base ``post_input`` dict is assembled so that
        required per-platform fields (e.g. Instagram/Facebook ``type``) are
        present before the mutation is sent.
        """
        meta_key = BufferService._service_to_metadata_key(service)
        if meta_key is None:
            return

        post_input.setdefault("metadata", {})
        existing = post_input["metadata"].get(meta_key, {})

        if service == 'instagram':
            existing.setdefault("type", "post")
            existing.setdefault("shouldShareToFeed", True)
        elif service == 'facebook':
            existing.setdefault("type", "post")
        elif service == 'tiktok':
            pass
        elif service == 'twitter':
            pass

        post_input["metadata"][meta_key] = existing

    def test_connection(self):
        """Validate the API key and return the account, including organizations."""
        query = """
        query GetAccount {
          account {
            id
            email
            name
            organizations {
              id
              name
            }
          }
        }
        """
        data = self._request(query)
        return data.get("account")

    def get_organizations(self):
        """Return the organizations available to this API key."""
        account = self.test_connection()
        return (account or {}).get("organizations", [])

    def get_channels(self, organization_id):
        """Return channels (connected social profiles) for an organization."""
        query = """
        query GetChannels($organizationId: OrganizationId!) {
          channels(input: { organizationId: $organizationId }) {
            id
            name
            displayName
            service
            avatar
            isQueuePaused
          }
        }
        """
        data = self._request(query, variables={"organizationId": organization_id})
        return data.get("channels", [])

    def _create_post(self, channel_id, text, mode, due_at=None,
                      metadata=None, image_url=None, save_to_draft=False,
                      service=None):
        if not channel_id:
            raise ValueError("channel_id is required")
        if not text:
            raise ValueError("Post text is required")

        post_input = {
            "text": text,
            "channelId": channel_id,
            "schedulingType": "automatic",
            "mode": mode,
        }
        if due_at:
            post_input["dueAt"] = due_at
        if metadata:
            post_input["metadata"] = metadata
        if image_url:
            post_input["assets"] = [{"image": {"url": image_url}}]
        if save_to_draft:
            post_input["saveToDraft"] = True

        if service:
            self._add_platform_metadata(post_input, service)

        query = """
        mutation CreatePost($input: CreatePostInput!) {
          createPost(input: $input) {
            ... on PostActionSuccess {
              post {
                id
                text
                dueAt
              }
            }
            ... on MutationError {
              message
            }
          }
        }
        """
        data = self._request(query, variables={"input": post_input})
        result = self._raise_if_mutation_error(
            data, "createPost", "Buffer did not return a post creation result"
        )
        return result.get("post", result)

    def queue_post(self, channel_id, text, metadata=None, image_url=None,
                    service=None):
        """Add a post to the channel's queue (next available time slot).

        Parameters
        ----------
        channel_id : str
            Buffer channel ID to post to.
        text : str
            Post body text.
        metadata : dict or None
            Arbitrary extra ``CreatePostInput`` fields.
        image_url : str or None
            Public URL of an image to attach.
        service : str or None
            Channel service name (e.g. ``"instagram"``, ``"facebook"``).
            When provided, platform-required fields (e.g. Instagram
            ``type``) are injected automatically.
        """
        return self._create_post(
            channel_id, text, mode="addToQueue",
            metadata=metadata, image_url=image_url,
            service=service,
        )

    def schedule_post(self, channel_id, text, due_at, metadata=None,
                       image_url=None, service=None):
        """Schedule a post for a specific ISO 8601 UTC timestamp, e.g. '2026-07-10T15:00:00.000Z'."""
        if not due_at:
            raise ValueError("due_at is required when scheduling a Buffer post")
        return self._create_post(
            channel_id, text, mode="customScheduled", due_at=due_at,
            metadata=metadata, image_url=image_url,
            service=service,
        )

    def draft_post(self, channel_id, text, metadata=None, image_url=None,
                    service=None):
        """Save a post as a draft rather than queueing/scheduling it."""
        return self._create_post(
            channel_id, text, mode="addToQueue",
            metadata=metadata, image_url=image_url, save_to_draft=True,
            service=service,
        )

    def publish_now(self, channel_id, text, metadata=None, image_url=None,
                     service=None):
        """
        Best-effort 'publish immediately'.

        The current beta GraphQL API has no documented immediate-publish
        mode - only addToQueue and customScheduled. This schedules the
        post for "now" via customScheduled, which is the closest
        equivalent, but Buffer may still place it in the next open
        posting slot rather than sending it instantly. Verify actual
        behavior against your account before relying on this for
        time-sensitive posts.
        """
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return self.schedule_post(
            channel_id, text, due_at=now_iso, metadata=metadata,
            image_url=image_url, service=service,
        )

    def get_posts(self, organization_id, channel_id=None, first=20, after=None):
        """List posts for an organization, optionally filtered by channel, with cursor pagination."""
        query = """
        query GetPosts($first: Int, $after: String, $input: PostsInput!) {
          posts(first: $first, after: $after, input: $input) {
            edges {
              node {
                id
                text
                dueAt
                status
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        """
        post_input = {"organizationId": organization_id}
        if channel_id:
            post_input["filter"] = {"channelIds": [channel_id]}

        variables = {"first": first, "after": after, "input": post_input}
        data = self._request(query, variables=variables)
        return data.get("posts", {})

    def delete_post(self, post_id):
        """
        Delete a post by id.

        The exact DeletePostInput shape isn't confirmed here - if this
        errors with a schema validation message, check the current
        DeletePostInput fields in the API Explorer at
        https://developers.buffer.com and adjust the variables below.
        """
        if not post_id:
            raise ValueError("post_id is required")

        query = """
        mutation DeletePost($input: DeletePostInput!) {
          deletePost(input: $input) {
            ... on DeletePostSuccess {
              id
            }
            ... on MutationError {
              message
            }
          }
        }
        """
        data = self._request(query, variables={"input": {"id": post_id}})
        return self._raise_if_mutation_error(
            data, "deletePost", "Buffer did not return a delete result"
        )