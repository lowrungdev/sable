# Nextcloud Talk API reference (user account)

Fetched 2026-10-01 from https://nextcloud-talk.readthedocs.io/en/latest/ (via WebFetch, so a small
model summarised each page: recheck anything surprising on the page itself). **Refresh** by
re-fetching the pages below, diffing against this file, and updating it plus `Fetched` above.
Pages: `/global/`, `/conversation/`, `/chat/`, `/reaction/`, `/capabilities/`, `/participant/`
(the index also lists avatar, call, poll, settings, integration, webinar).
sable's code is `src/sable/talk.py`, `events.py` and `poller.py`.

## Base paths: not one version for everything
- **Conversations: `/ocs/v2.php/apps/spreed/api/v4`** (v1-v3 are gone; v1 `/room` answers 404, OCS
  status 998 — this bit sable on first run, 2026-10-01).
- **Chat: `/ocs/v2.php/apps/spreed/api/v1`** (since NC 13). **Reactions: `api/v1`** (NC 24).
- Who am I: `/ocs/v2.php/cloud/user`. Capabilities: `/ocs/v2.php/cloud/capabilities` (`spreed` key).
- Files share (chat attachment): `/ocs/v2.php/apps/files_sharing/api/v1/shares`, `shareType=10`,
  `shareWith=<token>`, `talkMetaData` JSON (`messageType`, `caption`, `replyTo`, `silent`, `threadId`).
- Send `OCS-APIRequest: true` and `Accept: application/json` (XML is not guaranteed valid for rooms).
  The global page does not document auth headers; Basic auth with an app password is what sable uses.

## GET /room (v4)
- Params: `noStatusUpdate`, `includeStatus`, `modifiedSince` (only rooms with newer `lastActivity`;
  "use cautiously"). Headers: `X-Nextcloud-Talk-Hash`, `X-Nextcloud-Talk-Modified-Before` (feed to next
  `modifiedSince`). Docs advise a full refresh every 5 minutes regardless.
- Room fields we use: `token`, `type`, `name`, `displayName`, `lastActivity` (UTC s), `lastMessage`
  (object with `id`; omits `parent`/`reactionsSelf`), `readOnly`, `permissions`, `participantType`,
  `unreadMessages`, `unreadMention`.
- Types (constants page): 1 one-to-one, 2 group, 3 public, 4 changelog ("Talk updates"),
  5 former one-to-one (other user deleted), 6 note to self. The conversation page's summary said
  5 was note-to-self; the constants page is the authority. `objectType` `sample` marks the
  "Let's get started!" onboarding conversation (also: file, room, phone, event, extended_conversation…).
- Participant types: 1 owner, 2 moderator, 3 user, 4 guest, 5 guest moderator.

## GET /chat/{token} (v1) — the long poll
- `lookIntoFuture=1` waits; `timeout` default 30, **max 60** (only with lookIntoFuture=1);
  `lastKnownMessageId` offset; `limit` default 100, max 200; `setReadMarker` default **1** (send 0 to
  stay silent); `includeLastKnown` default 0; `noStatusUpdate`; `markNotificationsAsRead` default 1.
- Response header `X-Chat-Last-Given` = next offset. **304** = nothing new. 404 conversation gone,
  412 lobby active and not moderator.
- Message fields: `id`, `token`, `actorType` (users/guests/bots/deleted_users/federated_users…),
  `actorId`, `actorDisplayName`, `timestamp`, `messageType` (`comment`, `comment_deleted`, `system`,
  `command`), `message` (with `{placeholder}`s), `messageParameters`, `systemMessage` ('' for normal),
  optional `parent` (or `{id, deleted:true}`), `reactions` {emoji: count}, `reactionsSelf`,
  `referenceId`, `silent`, `isReplyable`, `markdown`, edit fields.
- Mention params are rich objects: `type` user/call/guest/user-group/…, `id`, `name`; mention text
  `@<id>` (quoted when it has spaces/slashes).

## POST /chat/{token}
Params `message`, `replyTo`, `referenceId` (SHA256), `silent`, `actorDisplayName` (guests only).
**201** on success. 400, 403 read-only room, 404, 412 lobby, **413 over 32000 chars**, 429 (guest
mention rate, 50/day). Delete (`DELETE /chat/{token}/{id}`, 6h) and edit (`PUT`, 24h) exist too.

## Reactions (v1, capability `reactions`)
- `POST|DELETE /reaction/{token}/{messageId}` with `reaction` **in the body**; `GET` lists
  (`reaction` as optional query filter). POST: 201 new, 200 already there. DELETE: 200.
  400 unsupported, **403 needs attendee permission 256**, 404 conversation/message/reaction missing.
- System messages: `reaction` (shown at first, "message will be replaced after action completes"),
  `reaction_deleted` (author removed it), `reaction_revoked` (a *moderator* removed someone else's).
  Docs say the `reaction` text is a `{reaction}` placeholder and the target sits in `parent`.

## Other system messages worth ignoring
conversation_*, call_*, user_added/removed, read_only, lobby_*, message_deleted/edited (state sync,
not shown), file_shared, poll_*, history_cleared, avatar_*, federated_user_*, message_expiration_*.

## Capabilities (names and first Talk version)
chat-v2 3.2, system-messages 4.0, mention-flag 4.0, chat-reference-id 9.0, reactions 14.0,
silent-send 15.0, chat-keep-notifications 16.0, edit-messages 19.0, delete-messages 11.1,
threads 22.0. Limits: `config.chat.max-length`, `config.chat.read-privacy`.

## Learned on a live server (2026-10-01)
- Seven simultaneous long polls took 41-90 s each against the stock Nextcloud docker image,
  whose PHP-FPM pool is `pm.max_children = 5` (start_servers 2): polls queue for a worker.
  With `pm.max_children = 32` all seven returned 304 in 30.8-31.0 s. A queued request can sit
  past the client's timeout, so ordinary posts can fail too while the pool is full.

## Verified on a live server (2026-10-01)
- A reaction added by a person arrives through the chat long poll as a `reaction` system message;
  sable's `_reaction()` recovers the emoji and `parent.id` is the reacted-to message. The docs'
  worry that it is replaced before a poll sees it, or that its text is only a `{reaction}`
  placeholder, did not bite. (Not recorded which of `message` or `messageParameters` carried it.)
- sable's own reaction (the thinking emoji) and reply posts work with the documented calls.

## Known unknowns (not verified against a live server)
1. Removal: whether `reaction_deleted` / `reaction_revoked` arrive through the poll, and in what
   shape. Nothing acts on them.
2. Whether a reaction moves a conversation's `lastMessage` in `GET /room` (matters only for a
   room-list polling design).
3. sable parses no removal at all: `parse_message` keeps the `reaction` system message and ignores
   every other one, `reaction_deleted` and `reaction_revoked` included. Nothing acts on a removal.
4. sable's DELETE sends `reaction` in the body, as documented, and also as a query parameter in
   case a server reads DELETE parameters only from the URL.
5. sable does not yet check the 256 reaction permission or the `reactions` capability.

## Verified against the docs 2026-10-01 (chat and participant pages)
- **Message context** (`chat` page): `GET /ocs/v2.php/apps/spreed/api/v1/chat/{token}/{messageId}/context`,
  capability `chat-get-context`. Param `limit` = messages "into each direction" (default 50, max 100).
  200 returns an array of chat messages (same shape as the poll) around the requested one; 404 when
  the conversation is not found for the participant; 412 lobby active and not moderator. Headers
  `X-Chat-Last-Given` and `X-Chat-Last-Common-Read`. **There is no single-message endpoint**; sable's
  `TalkClient.message()` calls this with `limit=3` (`CONTEXT_LIMIT`) and picks the entry whose `id` matches (none ->
  treated as not found). The docs do not say outright that the centre message is in the array: the code
  does not assume it. Unverified live: whether the message itself is included in the answer.
- **Leave a conversation** (`participant` page): `DELETE /ocs/v2.php/apps/spreed/api/v4/room/{token}/participants/self`.
  200 left; 400 when the caller is a moderator/owner and no other moderator/owner remains; 404
  conversation not found for the participant. (403 is listed only for removing *other* attendees.)
  sable treats 400/403 as "cannot leave", 404 as "already gone".
- **Mentions** (`chat` page): in message text a mention is `@<id>`; ids with a space or slash are
  wrapped in double quotes: `@"space user"`, `@"guest/random-string"`. Autocomplete `source` values:
  `users`, `federated_users`, `group`, `guests`, `calls` ("mentioning the whole conversation").
  The page documents **no explicit `@all` or team syntax**; `@all`, `@"group/..."` and `@"team/..."`
  are what Talk's clients send, so `sable.mentions.defang_mentions` covers them on that basis.
