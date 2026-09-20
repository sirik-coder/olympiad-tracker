# Olympiad tracker

Watches the 46th FIDE Chess Olympiad live on Lichess, finds the moves worth
talking about, and posts them to Slack with a draft caption in ChessMood's
voice.

---

## What it does, in order

1. GitHub Actions tries to start a run every hour through the round. Whichever
   one actually gets a machine polls every three and a half minutes until the
   round is over; the rest exit in seconds.
2. Each poll asks Lichess which round each of the nine Olympiad broadcast
   groups is playing right now.
3. It downloads each group's games as PGN and keeps only the ones with a player
   we watch.
4. For every new move, it compares the position before and after and decides
   whether it was a clear blunder or a confirmed brilliancy.
5. Anything it finds goes to the `#chess-olympiad` Slack channel with the names,
   the board, the move, a plain description, a link to the live game, and a
   draft social caption.
6. It writes down how far it got, so the next run does not repeat itself.

---

## Setting it up

You need three things: a Slack webhook, a GitHub repository, and one secret.

### 1. The Slack webhook

In Slack, create the channel `#chess-olympiad`, then go to
**api.slack.com/apps → Create New App → From scratch**, pick your workspace,
open **Incoming Webhooks**, turn it **On**, and click
**Add New Webhook to Workspace**. Choose `#chess-olympiad` and click **Allow**.

You get a URL that starts with `https://hooks.slack.com/services/`. Treat it
like a password: anyone who has it can post to your channel.

### 2. The secret

In the GitHub repository, go to
**Settings → Secrets and variables → Actions → New repository secret**.

- **Name:** `SLACK_WEBHOOK_URL`
- **Secret:** the URL from step 1

The webhook is never written into any file in this project. It is read from the
environment at run time and is kept out of logs and error messages.

### 3. Make the repository public

GitHub Actions minutes are free and unlimited on public repositories. On a
private one they are not, and this job runs for about eight hours a day for
twelve days, which is around 5,700 minutes — well past the 2,000 free minutes a
private repository gets each month.

Nothing secret lives in the code, so public is safe here. If the repository must
stay private, expect a bill of roughly $30 for the event.

---

## Who gets watched

Edit `watchlist.yml`. Nothing else needs changing.

```yaml
min_rating: 2600          # Open event
min_rating_women: 2400    # Women's event, which is rated lower throughout

always_watch_players:
  - name: "Laohawirapap, Prin"
    fide_id: 6205003

always_watch_teams: []
mute_players: []
```

Two things worth knowing:

**Use `fide_id` where you can.** Names are spelled differently in different
places - Lichess writes `Sargissian, Gabriel` for the player you might write
down as Gabriel Sargsyan. A FIDE id never changes and is never ambiguous. You
can read anyone's id straight from the live feed:

```bash
python tools/find_player.py "Sargsyan"
```

**The Women's event needs its own number.** The highest rated woman in this
Olympiad is Bibisara Assaubayeva at 2536, so a single 2600 cutoff would mean the
Women's Olympiad never produces one alert. At 2400 it produces about 21 games a
round.

---

## Checking the thresholds before going live

Run the detector over a round that has already finished. It cannot post to
Slack - there is no Slack code in that file at all.

```bash
python backtest.py --round "Round 2" --compare        # how many alerts per setting
python backtest.py --round "Round 2"                  # the alerts themselves
python backtest.py --round "Round 2" --sensitivity strict
python backtest.py --round "Round 2" --group Women
```

The three settings:

| Setting    | Rule                                                     |
|------------|----------------------------------------------------------|
| `loud`     | winning chances drop 20+, from 40%+, down to 35% or worse |
| `balanced` | drop 25+, from 45%+, down to 30% or worse                 |
| `strict`   | drop 35+, from 50%+, down to 22% or worse                 |

**`loud` is what runs live.**

Change the setting for a live run in
**Actions → Olympiad live tracker → Run workflow → sensitivity**.

---

## How a blunder is decided

Not in centipawns. A centipawn is not worth the same everywhere: going from
+5.0 to +12.0 is a 700-point swing that changes nothing, while 0.0 to -2.0 is a
200-point swing that decides the game. Setting one threshold in centipawns would
either flood the channel with alerts from won positions or miss the real ones.

So every evaluation is first converted to **winning chances**, a 0-100 scale
that moves fast near equality and barely moves once someone is winning. A forced
mate is simply 100 or 0.

A move is a blunder when the player's winning chances fall far enough, **and**
the position changes category:

- **threw the game** - was at least equal, is now losing
- **threw away a win** - was winning, is not any more

## How a brilliancy is decided

Harder, and it needs the engine. A brilliancy is not just a sacrifice that
worked; it is a sacrifice that was the *only* thing that worked. All four have
to be true:

1. The move gives up real material - what it captures, minus what the opponent
   can win back **anywhere on the board**, by static exchange evaluation. Both
   halves matter. Ignore what the move takes and every routine recapture looks
   like a sacrifice; look only at the square it lands on and most real
   sacrifices are invisible, including Levon Aronian's 40...Kh7 in Round 4,
   a king move that gave up 3.2 pawns sitting elsewhere.
2. The position is still good for the player afterwards.
3. Their evaluation did not drop.
4. Stockfish agrees it is the best move **and** the second-best move is clearly
   worse.

Step 4 needs the best move *and* the runner-up in the same position, which no
feed provides.

---

## Where the evaluations come from

This one is worth knowing, because it is easy to assume the opposite.

The broadcast PGN does carry Lichess's own Stockfish evaluation after almost
every move — **but only once a game has finished**. While a game is being
played, its moves arrive with clock times and nothing else. Checked against two
other live broadcasts: every in-progress game had zero evaluations, every
finished game had them on nearly every move.

So:

- **Live** (`run.py`): every number comes from our own Stockfish.
- **Finished rounds** (`backtest.py`): the feed's evaluations are used, which is
  why a backtest over a whole round takes about three minutes instead of an hour.

Live analysis runs in three passes, to keep a poll inside its time slot:

| Pass | Runs on | Depth | Why |
|---|---|---|---|
| screen | every new move | 12 | enough to say "something happened here", about 0.1s each |
| verify | only flagged moves | 18 | shallow searches are noisy, and a false alert costs more than a missed one |
| confirm | only moves about to be alerted | 22 | depth 18 is not always enough to be sure |

Positions are cached between passes, because the position after one move is the
position before the next.

Two rules keep this honest, and both were learned the hard way when the first
version of it found **none** of three known blunders in a test round:

**The screen is deliberately loose.** It passes anything within 15 points of the
alert threshold through to the deep check. A cheap filter that applies the real
rule throws away real findings, because the shallow number it is judging is the
unreliable one.

**The last pass is deeper than it looks like it needs to be.** There is an
endgame in Round 2 that Lichess scores as mate-in-10 becoming mate-in-9 —
nothing happening at all. At depth 18, from a cold hash table, Stockfish
evaluates it as exactly **0.00**, because it believes White has a perpetual
check. That reads as a player throwing away a win, and the alert fires. At
depth 22 it evaluates it correctly and the alert does not.

The same position came out at −7.06 at depth 18 in an isolated test, which made
the bug look fixed when it was not — that process had a warm hash table from
earlier searches. Anything measured on one lucky search is not measured. Depth
22 costs 2–3 seconds a position, and it runs on perhaps two moves per poll.

**Search limits are set by depth, not by a tight clock.** A search capped at
250ms returns whatever it reached, which depends on how busy the machine is —
and it fails towards silence, reporting a position as calmer than it is. One
real collapse from 0.84 to -3.15 was read as 0.76 to -2.03, landing just inside
the threshold, and no alert fired. The millisecond figures in the config are a
safety net so one wild position cannot stall a poll, not the actual limit.

---

## Why it polls instead of streaming

Lichess offers a streaming PGN endpoint that pushes moves as they are played.
A cron job cannot use it: the job wakes up, looks, and exits, and cannot hold a
connection open in between. The plain snapshot endpoint already contains
whatever the stream would have pushed while we were asleep, so nothing is lost.

## Scheduling, and two rounds' worth of getting it wrong

GitHub does not start scheduled jobs when it says it will. Two designs failed
on live rounds before this one.

**Every fifteen minutes, polling for thirteen.** On 18 September GitHub
delivered three of the thirty-two runs, and started one at 19:56 UTC, two hours
after the window closed. Eight hours of chess got forty minutes of watching.

**Two long runs a day.** On 19 September the 10:00 run started at **13:45** —
three and three quarter hours late. The 15:35 run then queued behind it, started
at 19:15 once it finished, and watched an empty board for 165 minutes. Every
alert that round came from one section, because the other sections' decisive
moments had happened during the unwatched hours.

**What runs now:** an attempt every hour through the round, each polling until a
fixed wall-clock time rather than for a fixed length. Whichever run actually
gets a machine covers the rest of the day; the ones queued behind it wake up
past the end time and exit in seconds. The hourly crons also hand over when a
run hits GitHub's six-hour ceiling.

Within a run, each sweep of the nine groups takes about 80 seconds with a 90
second gap after it, so a game is looked at roughly every three and a half
minutes.

**A late start now catches up the whole round.** There used to be a 40 half-move
limit on how far back a game was read when first seen, meant to protect the
engine budget on a cold start. Combined with a 3h45m late start it meant the
first half of every game was never analysed - not found clean, never looked at.
The limit is gone and the engine budget raised to match.

---

## Running it on your own computer

```bash
pip install -r requirements.txt
python backtest.py --round "Round 2" --no-engine     # no Stockfish needed
```

For the engine, install Stockfish:

- macOS: `brew install stockfish`
- Ubuntu/Debian: `sudo apt install stockfish`
- Windows: download from stockfishchess.org, then
  `set STOCKFISH_PATH=C:\path\to\stockfish.exe`

To watch live games without posting anything:

```bash
python run.py --once --dry-run
```

---

## Files

| File | What it is |
|---|---|
| `run.py` | the live job: poll, detect, post |
| `backtest.py` | run over a finished round, print, never post |
| `watchlist.yml` | who gets watched |
| `tools/find_player.py` | look up a player's FIDE id and group |
| `olympiad/lichess.py` | talking to the Broadcast API |
| `olympiad/pgnfeed.py` | PGN into games and moves |
| `olympiad/detect.py` | the blunder and brilliancy rules |
| `olympiad/engine.py` | Stockfish, and the material sacrifice test |
| `olympiad/evalscale.py` | centipawns into winning chances |
| `olympiad/captions.py` | caption drafts in ChessMood's voice |
| `olympiad/slack.py` | the webhook |
| `olympiad/state.py` | what we have already seen |

---

## Things that are true about this event and not obvious

- The Olympiad is **nine separate Lichess broadcasts**, held together by a
  group. The code in the index URL (`32sSzO6S`) is the *group* id, and
  `/api/broadcast/32sSzO6S` returns 404. The way in is to ask any member
  tournament for its record, which carries the list of all nine.
- The groups are split by **match number**, and match numbers follow the
  standings, which change every round. A team is not in a fixed group. Thailand
  sat in *Open | Matches 38-62* in round 2 and will move. The tracker reads all
  nine groups every time rather than looking a team up.
- Each group keeps **its own round ids**. There are 99 of them. They are
  resolved at run time.
- Rounds start at **10:15 UTC**, except **round 11 on 27 September, which starts
  at 06:15 UTC**. The rest day is **22 September**.
