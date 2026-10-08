# Notifications

Alerts are set up on the *Notifications* tab in three parts:

- **Providers** are destinations: one WhatsApp group, one Telegram chat, one
  webhook. Add one provider per group or chat.
- **Rules** decide **what** is sent, **how urgent** it is and **to which
  providers**.
- The **WhatsApp phone** box links the app to your phone, for the `linked`
  WhatsApp provider.

Every message and its delivery result is recorded in the alert log at the
bottom of the tab.

## Rules

| Send when | Kind | Fires |
|---|---|---|
| Scrap rate ≥ threshold | state | The current job's scrap rate (as shown on the dashboard) reaches the threshold. Entered in %, e.g. `5` |
| Fail (NOK) count ≥ threshold | state | The current job's NOK count (as shown on the dashboard) reaches the threshold |
| Station's device disconnected or read error | state | A device the station uses can't be read, has gone offline, or doesn't deliver the value |
| Station stopped, idle or back in production | event | A station's [production state](user-guide.md#production-state) changes |
| Station changed job | event | A station's job changes |
| FTP backup failed / FTP backup finished | event | An automatic or *Run now* FTP backup ends |
| App started or updated | event | The app starts; after an image update the message says which version it was updated from. Sent about a minute after startup, so a linked WhatsApp has reconnected |

For each rule you choose:

- **Station**: one station or all stations (station conditions only).
- **Level**: ℹ️ INFO, ⚠️ WARNING or 🚨 ALERT. It is put in front of the
  message, e.g. `⚠️ WARNING · High scrap on 'Line 1' …`.
- **Threshold** for scrap and fail-count rules. A rule for **all stations**
  can set a different threshold for individual stations ("Per station"); an
  empty box uses the rule's threshold.
- **Send to**: tick the providers that get the message. With none ticked it
  goes to every enabled provider (rules made before this option existed work
  that way, so they keep sending after an update).
- **Cooldown**: state rules fire again after this many seconds while the
  condition still holds (default 300). Event rules fire each time the event
  happens; a cooldown only limits how often.

Rules can be edited, disabled and enabled again without deleting them.

Typical setup: a *warning* rule at 3 % scrap to the shift group, an *alert*
rule at 5 % to the shift group and the maintenance group, disconnects to
maintenance only, and backup and app messages to an admin chat.

Scrap-rate and fail-count rules are suppressed while a station is idle or
manually stopped. They use the counters shown on the dashboard, so
**Reset counters** on the station view also clears them. A disconnect rule
fires when a device the station uses is offline or can't deliver its value.

Rules are per station since 1.5; rules made for a device in earlier versions
now belong to the station the device became. Messages say "Station 'M1' …"
and name the app Scrap Meter.

## WhatsApp

The WhatsApp provider (`app/notifiers/whatsapp.py`) has four transports. Use
**Test send** after adding a provider to confirm delivery.

### `linked`: send from your own number (no extra service)

> Since 1.21 this is the **WhatsApp virtual client** in Settings → Developer
> options (administrators); it is off on a new install. While it is off the
> WhatsApp phone card is hidden and `linked` providers don't send.

> **Unofficial.** The app logs in to WhatsApp as a *linked device* of your
> phone, the same way WhatsApp Web does, using the open-source
> [neonize](https://github.com/krypton-byte/neonize) / whatsmeow library.
> WhatsApp does not allow unofficial clients and can **restrict or ban a
> number** that uses one. A few alert messages a day are low risk, but a spare
> number (a cheap SIM, or WhatsApp Business on a second number) is safest.

1. On the Notifications tab, press **Link phone** in the *WhatsApp phone* box.
   A QR code appears.
2. On the phone open WhatsApp → **Settings → Linked devices → Link a device**
   and scan the code. It changes every 20 seconds; the page keeps it current.
   After about 3 minutes without a scan it expires; press **Link phone** again.
3. The box shows **Linked** and the number. Press **Show groups**, then
   **Send alerts here** next to a group. That adds a provider for the group.

The phone must be a member of every group it sends to. Messages appear as sent
by that number. To send to one person instead, use their number with country
code (digits only) as `to`:

```json
{"transport": "linked", "to": "120363012345678901@g.us"}
{"transport": "linked", "to": ["421900123456", "120363012345678901@g.us"]}
```

The login is stored in the app's own PostgreSQL database (tables starting with
`whatsmeow_`), so it survives restarts and image updates. The phone does not
have to stay online, but WhatsApp logs out linked devices when the phone has
not been used for about 14 days. **Log out** in the box, or removing *Scrap
Meter* from the phone's Linked devices, unlinks it. (A phone linked before the
rename keeps showing *Cognex Monitor* there until it is linked again; that is
only the name.) If the connection drops,
the app reconnects by itself and the box shows the error meanwhile.

The client runs in a separate process in the app container. Its log lines are
in `docker compose logs web`, starting with `whatsapp`.

### Commands in WhatsApp groups

With a phone linked, people in a WhatsApp group can ask the app for figures:
send `!status` in the group and the linked phone answers with every station's
production state, OK / NOK and scrap. `!status line 1` answers for the stations
whose name contains "line 1" only, and `!help` lists the commands allowed in
that group.

Commands are set up in the **Chat commands** section of the Notifications
tab. They work the same in [Discord](#discord) channels of a bot and
[Signal](#signal) groups of the linked Signal phone; **Answers in** lists the
WhatsApp groups, Discord channels and Signal groups to choose from. A `status` command is there from the start.

- **Prefix**: a message only counts as a command when it starts with it (`!`
  by default; 1-3 characters starting with a symbol, e.g. `/` or `#cm`), so
  normal chat never triggers anything.
- **Keyword**: the word after the prefix (lower case letters, digits, `-`,
  `_`).
- **Counts**: *dashboard counters* (current job, since the last reset, as on
  the dashboard), *today* (production time only, the same figures as Scrap
  statistics) or *the last N hours*.
- **Stations**: *All stations*, or *Only stations active recently* with a
  number of days. Then the reply lists only the stations that were in
  production at some point in that many days (running now, or their OK counter
  last went up while in production within that time); the others are left
  out, also from the totals and `{stations}`. When none qualifies, the reply
  says "No station was in production in the last N days." `!status line 1`
  for a station outside that time answers that no such station matches.
  `{active_days}` shows the setting in the header or footer.
- **Header, one line per station, footer**: the reply text. `{placeholders}`
  are filled in; the dialog lists them (`{station}`, `{state}`, `{job}`,
  `{pass}`, `{fail}`, `{scrap}`, `{total_scrap}`, `{date}`, `{time}`, …).
  `{device}`, `{camera}`, `{devices}` and `{cameras}` from earlier versions
  still work and mean the station.
  **Preview reply** shows what it would send now, without sending.
- **Totals** (`{total_pass}`, `{total_fail}`, `{total}`, `{total_scrap}`)
  leave out stations excluded from the statistics (see the
  [User guide](user-guide.md#stations-excluded-from-the-statistics)). Their
  line still appears, ending in "(not in totals)". With *today*, readings a
  user included count, as on Scrap statistics.
- **Answers in**: tick the groups where the command works. None ticked = every
  group the phone is in. In other groups the app stays silent, also for
  `!help` and unknown commands. Private chats are never answered.

Times in a reply use the time zone of the browser that last saved the command.
A group gets at most one answer every 3 seconds. Every command the app
receives is listed in the **Command log** with the group, the sender, the
reply or the reason it was not answered. Messages older than two minutes
(delivered after the app was offline) are ignored.

**Muting a station**: `!mute line 1` mutes the alerts of the station called
"Line 1" (any case; part of the name works when only one station matches)
until its job changes; `!unmute line 1` turns them on again. Without a name,
or with one that matches no single station, the reply lists the stations. Both
work for anyone in a group where any command is allowed, are listed by
`!help`, and do the same as the Scrap warnings switch on the station view
(see the [User guide](user-guide.md#station-view)). A command you set up with
the keyword `mute` or `unmute` replaces the built-in one.

Commands also work when you send them from the linked phone itself, and from
the [Chat room](user-guide.md#chat-room) tab when the room is a WhatsApp group;
the app's own replies never trigger a command.

### `webhook`

POSTs the message as JSON to any HTTP endpoint, for example a self-hosted
gateway built on whatsapp-web.js, Baileys or WAHA.

```json
{"transport": "webhook", "url": "http://gateway:3000/send",
 "headers": {"Authorization": "Bearer ..."},
 "payload_key": "message", "extra": {"chatId": "1234567890-123456@g.us"}}
```

### `greenapi`

Uses the third-party [Green API](https://green-api.com) group-send endpoint.

```json
{"transport": "greenapi", "id_instance": "...", "api_token": "...",
 "group_id": "1234567890-123456@g.us"}
```

### `cloud_api`

Meta's official Cloud API. Sends to **one phone number only**, not a group;
Meta's API can't post to WhatsApp groups.

```json
{"transport": "cloud_api", "token": "...", "phone_number_id": "...",
 "to": "<recipient number>"}
```

## Telegram

The Telegram provider (`app/notifiers/telegram.py`) posts through a bot,
which is Telegram's official way to do this.

1. In Telegram, talk to **@BotFather**, send `/newbot` and copy the token it
   gives (looks like `123456789:AAH...`).
2. Add the bot to the group (or open a chat with the bot and press *Start*).
3. Find the chat id: send any message in the group, then open
   `https://api.telegram.org/bot<token>/getUpdates` in a browser and look for
   `"chat":{"id":...}`. Group ids are negative, e.g. `-1001234567890`. A public
   channel can be given as `@channelname` (the bot must be an admin there).
4. Add a provider of kind **Telegram**:

```json
{"bot_token": "123456789:AAH...", "chat_ids": ["-1001234567890"]}
```

`chat_ids` can list several chats; each gets the message. Add
`"silent": true` to send without a notification sound. **Test send** shows
Telegram's own error if something is wrong ("chat not found" means the bot is
not in that chat).

A Telegram chat can also be the [Chat room](user-guide.md#chat-room). While it
is, the app asks Telegram for the bot's new messages (`getUpdates`), so the bot
must not have a webhook set or be read by another program at the same time.
Step 3 then shows nothing new: find chat ids before choosing the room, or with
the room set to another chat.

## Discord

The Discord provider (`app/notifiers/discord.py`) has two modes.

**Bot** (default): a Discord bot posts into channels of your server and reads
them, so chat commands (`!status`, `!mute Line 1`) and the
[Chat room](user-guide.md#chat-room) work in those channels.

1. Open the Discord developer portal (discord.com/developers/applications),
   **New Application**, then **Bot**: copy the token (**Reset Token**).
2. On the same Bot page turn on **Message Content Intent** (Privileged
   Gateway Intents). Without it the bot gets messages without their text and
   cannot answer commands or show messages in the Chat room.
3. **OAuth2 → URL Generator**: scope `bot`, permissions *View Channels*,
   *Send Messages* and *Read Message History*. Open the URL and add the bot
   to your server.
4. In Discord turn on **Developer Mode** (Settings → Advanced), then right
   click a channel → **Copy Channel ID**.
5. Add a provider of kind **Discord**:

```json
{"mode": "bot", "bot_token": "MTIz...", "channel_ids": ["1234567890123456789"]}
```

The app reads the channels by asking Discord for new messages every few
seconds; it starts with the newest message, so nothing written before the app
started is answered. Messages longer than Discord's 2000 characters are sent
in parts.

**Webhook**: only sends, no bot. In the channel's settings → Integrations →
Webhooks → **New Webhook** → **Copy Webhook URL**:

```json
{"mode": "webhook", "webhook_url": "https://discord.com/api/webhooks/..."}
```

## Signal

Signal has no bot API. The app sends through
[signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), which
runs next to it as its own container and is linked to a phone as a secondary
device, like Signal Desktop. Alerts are sent from that phone's number, and
the app reads the groups the phone is in for chat commands and the Chat room.
A spare number is a good idea, as with WhatsApp.

Signal is a Developer option (Settings → Developer options → **Signal
(signal-cli)**, administrators): turn it on first; while it is off the app
doesn't use the Signal service and the Signal card is hidden.

Setup on the server, once:

1. Copy `deploy/docker-compose.signal.yml` next to `docker-compose.yml` and
   start both: `docker compose -f docker-compose.yml -f docker-compose.signal.yml up -d`
   (or rename it to `docker-compose.override.yml` if you have no override
   file yet; then a plain `docker compose up -d` reads it). It adds the
   `signal` container (no port is published) and sets `SIGNAL_API_URL` for
   the app. Keep using the same command for later updates.
2. On the Notifications tab, **Signal phone → Link phone** shows a QR code.
   On the phone open Signal → Settings → **Linked devices** → **Link new
   device** and scan it.
3. **Show groups** lists the phone's groups; **Send alerts here** adds a
   provider for one. A provider can also be written by hand:

```json
{"to": ["group.abc123=", "+421900123456"]}
```

To unlink, remove *Scrap Meter* from the phone's Linked devices. The link is
kept in the `signal_data` volume.

## Credentials

Provider settings, including tokens, are stored in the app's database and are
part of backups. Protect database access and backup files accordingly.

## Adding a provider type

Notifiers follow the same one-file pattern as protocols: add a file in
`app/notifiers/` implementing the `Notifier` base class and register it in
`app/notifiers/__init__.py`.
