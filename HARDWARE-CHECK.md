# Hardware check (Watch only)

This checks that C3 Life can see and read the real booth gear: the Wing, the Tio1608-D, ProPresenter and anything else on the booth network. You don't need the HP for it. Use any Mac or Windows laptop.

**It changes nothing on any device.** It only asks questions. Every Wing message it sends must be a question with no value, and every Tio line must be a `get`. Anything else is refused and counted in the report, and the report should say 0.

## What you need

- A laptop plugged into the booth switch with a cable. Wi-Fi isn't on the booth network.
- Python 3.9 or newer.
  - **Mac:** open Terminal and type `python3 --version`. If it offers to install the developer tools, say yes.
  - **Windows:** install it from python.org and tick "Add python.exe to PATH".
- This folder (`c3life-server`) copied onto the laptop. Unzip `c3life-server.zip` if that's how you have it.
- A quiet time, not during a service. It doesn't change anything, but you'll want to look at the desk while it runs.

## Run it

1. Open Terminal (Mac) or PowerShell (Windows) in the `c3life-server/server` folder.
2. Run:
   - Mac: `python3 hwcheck.py`
   - Windows: `py hwcheck.py`
3. If the laptop asks whether Python may accept incoming connections, choose **Allow**. On Windows, choose Private networks. The Wing's answers and meters come back that way.
4. It takes about a minute:
   - It scans the network (only asks).
   - It watches the Wing for 20 seconds.
   - It watches the Tio for 20 seconds.
   - It reads ProPresenter's version.
5. It saves a report, `hwcheck-report-<date>.txt`, in that folder. **Send that file back.** It has names and addresses, but no passwords.

If the scan doesn't find something, give its address:

```
python3 hwcheck.py --wing 192.168.10.20 --tio 192.168.10.41 --propresenter http://192.168.10.30:50001
```

## What to check while it runs

| Check | What good looks like |
|---|---|
| Network scan | It lists the Wing, the Tio, ProPresenter (on the Mac mini) and the Dante devices. Anything listed as "No driver" is a device C3 Life doesn't know. |
| Wing names and faders | The first 12 named channels match the desk: names, fader levels, mutes. |
| Wing meters | "Meters arrived for 20 of 20 seconds". Play something into a channel so the meters move. |
| Wing preamps | The gain and 48 V for the first inputs match the desk. |
| Wing card outputs | "Dante card outputs read: 64 of 64", and the outputs in use (for example Card out 33 sends MAIN 1). This proves C3 Life can do *Set it up for me* on this Wing. |
| Tio | "Inputs read: 16 of 16". Gain and 48 V match R Remote. |
| Nothing changed | "0 changes refused", for both the Wing and the Tio. |

Then:

1. Move one fader on the desk, and change one gain in R Remote.
2. Run the check again.
3. Check that the new values show in the report. That proves C3 Life reads live changes.

## If something says `!!`

- **Wing: no answer on OSC port 2223.**
  - Check the Wing's remote settings. OSC must be allowed from this laptop, or open.
  - Check the Wing's address on the desk.
- **Meters: 0 seconds.**
  - A firewall on the laptop is blocking incoming UDP. Allow Python and run it again.
- **Tio: no answer on TCP 49280.**
  - Is the Tio on?
  - Can R Remote see it from the same laptop?
- **ProPresenter: no answer.**
  - In ProPresenter, go to Settings > Network, turn on Enable Network, and use the same port.
- **Not found by the scan.**
  - The device is off, or on a different network than 192.168.10.x.
  - Or (for a Mac) it's asleep.

## What it doesn't test yet

- **Changing anything (Control).** That comes later, on the HP. It should be done with FOH watching, never during a service, one thing at a time (a fader, a mute, then gain, then phantom power with the mic's channel muted).
- **Lightkey and the Sennheiser receivers.** The scan doesn't recognise these yet.
