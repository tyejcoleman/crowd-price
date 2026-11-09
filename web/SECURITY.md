# Crowd Price web: what it guards, how to host it, and its known limits

This is the buyer's drop page and the maker's view over the engine (`web/crowdweb/`). It is built for the sandbox: test mode (a
stand-in for PayPal), or PayPal's sandbox with test cards. No real money moves, and every page says which PayPal it is on. Nothing here is
reviewed for real money.

## What the web layer promises

- It shows the engine's numbers and the engine's log. It invents none, and it says what became of a hold only as the log says it.
  A buyer whose leave PayPal would not release is not told "released"; a hold taken at PayPal outside the engine is not "released";
  a hold that lapsed is not "the drop missed its minimum".
- No route settles, closes, excludes or binds. Those move money, and only a person approves them in Togi.
- One join does not become two holds, and a hold is released only by the browser that placed it (tokens.py and `Api.join` say how).
  The page lets go of its secret only when the server says nothing was held (`nothing_held`).
- Limits on abuse, all in `server.py`: 64 connections at once, 16 per address from accept until the response is written (a join or
  leave that has won its drop's turn is the one exemption), a total deadline on every request, 12 joins a minute per address and
  120 per drop, 20 wrong maker keys a minute per address.

## Hosting

- By default the server listens on this machine only and accepts only this machine's own Host names.
- Hosted behind a proxy or a tunnel that you control: start it with `--trust-proxy` (and `--public-host your.name`). The client is then
  the right-most `X-Forwarded-For` entry, which the proxy must append itself. The server's per-address connection cap is off in this
  mode, because every connection comes from the proxy, so **the proxy has to cap connections per client and buffer slow requests**.
- A tunnel or proxy **without** `--trust-proxy` makes every visitor one address. They then share one 16-connection allowance, one
  join allowance and one wrong-key budget, and one visitor's flood shuts all the others out. Do not host that way.

## The studio's approval desk: one limit to know

Togi tells your own approver from a test one by where its files are: the same directory by another path, a link to it, or its files
linked in. **A plain copy of your approver's files (not a link), used as a test approver (`TOGI_APPROVER_HOME`), is not detected.** The
desk then opens for it without a terminal, as it does for any test approver. What it signs is test scope, and Crowd honours test-scope
grants only in test mode, where no money moves; on the PayPal sandbox only your own approver's yes counts. Anything that
can read your files can already make a test approver of its own, so the copy adds nothing to that. It does mean "the desk opened
without a terminal" does not prove the approver is not yours.

## Known limits

These are real, rare, and not fixed. Each needs a coincidence that a demo does not make by itself, and the engine's own numbers stay
right in all of them. (F5 to F7 are the numbers the round-5 review gave them.)

1. **A same-name assistant join between this server's last look and the engine's lock (two cases).** The assistant is another process
   on the same store. If it joins the same name in the gap between the web's last read of the drop and the engine taking the drop's lock:
   - with an outage at PayPal, or a lost reply, on the web's own ask, both asks can carry one request id; the engine answers both with
     the one hold, and the web can bind that hold to the browser that asked second. That browser can then see it and release it,
     though the assistant's ask placed it (F5).
   - if the assistant's join simply completes, the web's own join is refused as taken, but the web keeps its claim. The visitor is then
     told their try "may have placed a hold" and not to join again, and the maker's view labels the assistant's buyer "(unconfirmed
     web join)". The words are wrong; no hold is moved (F6).

   Maker: if a buyer's name shows "(unconfirmed web join)" and you know the assistant added that name, ignore the label.
2. **The 10-second stamp rule depends on the wall clock (F7).** A claim is matched to the engine's join by time: a join stamped within
   10 seconds after the claim (1 second before) is the claim's own. If this server stalls longer than that between writing the claim
   and the engine stamping the join (a 10-second pause from garbage collection, CPU starvation, a sleeping laptop or a clock step),
   the join is no longer recognised as the claim's, and it takes a second event to hurt:
   - a lost PayPal reply: the retry is told "still being sorted out" with `nothing_held`, the page lets go, and a buyer who joins
     again under another name has two holds (the first is released when the drop closes);
   - or the server dying after the engine records the hold and before the web binds it: the retry is told the name is taken with
     `nothing_held`, and a buyer who joins again under another name is charged twice. Before settlement the maker can take one out
     (`drop exclude`); after it, the second charge is refunded in the PayPal sandbox.

   For a demo: keep the machine awake, do not kill the server mid-join, and keep a flood off it (see Hosting).
3. **A leave can release another hold of the same name (engine side).** A leave is checked against the browser's secret, then sent
   to the engine by name. If, in that gap, the assistant leaves that buyer and joins the same name for someone else, the engine
   releases the new hold. The engine's leave takes a name, not a hold, so the web cannot close this.
4. **A leave PayPal would not release can be followed by a second hold.** The page shows such a buyer the truth and offers no
   "Join again", but the engine lets a name that left join again, so another browser can place a second hold beside the first, which
   stays on the card until the maker has voided it at PayPal. The engine does not record the maker's void, so the web cannot tell
   when it is safe to refuse the name.
