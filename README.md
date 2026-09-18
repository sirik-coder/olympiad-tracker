# Olympiad tracker

Watches the 46th FIDE Chess Olympiad live on Lichess, finds the moves worth
talking about, and posts them to Slack with a draft caption in ChessMood's
voice.

---

## What it does, in order

1. Every 15 minutes during a round, GitHub Actions starts a run.
2. The run asks Lichess which round each of the nine Olympiad broadcast groups
   is playing right now.
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

1. The move gives up real material - checked with a static exchange evaluation
   on the destination square, which is cheap enough to run on every move.
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
| screen | every new move | 12 | enough to tell "equal" from "lost", about 0.1s each |
| verify | only flagged moves | 18 | shallow searches are noisy, and a false alert is worse than a missed one |
| confirm | only sacrifice candidates | 18, top 2 moves | was it the *only* move |

Positions are cached between passes, because the position after one move is the
position before the next.

---

## Why it polls instead of streaming

Lichess offers a streaming PGN endpoint that pushes moves as they are played.
A cron job cannot use it: the job wakes up, looks, and exits, and cannot hold a
connection open in between. The plain snapshot endpoint already contains
whatever the stream would have pushed while we were asleep, so nothing is lost.

## Why one run polls several times

GitHub will not schedule a job more often than every five minutes, and under
load it often starts one ten or twenty minutes late. A run that looked once
would turn "every 15 minutes" into "sometime within the half hour". Instead each
run polls every 150 seconds for about 13 minutes, so a late start costs a little
overlap rather than a gap.

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
