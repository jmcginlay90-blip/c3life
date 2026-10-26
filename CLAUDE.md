# Setting up the C3 Life server: notes for Claude

You are on (or helping with) the **HP EliteDesk in the C3 Life Leeds booth**. It runs the C3 Life app for the whole
booth. Someone from the church is with you. Their own guides, written for people, are the plan to follow:

- HP Set-up, Step by Step: https://claude.ai/artifact/W4ff6pSfy5fVwHyAKhST3R
- Dante and Wing Set-up: https://claude.ai/artifact/5g7YV8hQ9AsGqsFXVWhSzt
- C3 Life Manual (using it, and updating the app): https://claude.ai/artifact/Et6VmMcfA8zLa1sTWkCrKz
- The detailed set-up guide: https://claude.ai/artifact/YEsuoC1xJXBEBE38N1Zg4q
- `README.md` in this folder: every script, option and file, in detail

They are private links: if you can't open them, ask the person to paste the part you need. `README.md` has the same
steps.

## The rules (the church's own, keep to them)

1. **Ask before every change**, and say in plain words what it will change. Read-only checks can just run.
2. **Never on a Sunday morning, or during a service.** Set up and update on a weekday.
3. **Never type, ask for or store passwords, PINs or licence keys.** The person types them: the Audinate licence,
   booth's password in Autologon, the C3 Life PINs and Admin password (`--set-pin`, see README), and the GitHub key for
   updates (`Set-GitHubKey.ps1`, which asks for it itself).
4. **Nothing on the Sunday run sheet runs by itself.** Don't turn on anything that starts macros by itself. Recording is
   the one exception: it only listens.
5. **Devices start in Watch only.** Only an Admin, in the app, switches the Wing or the Tio to Control. Don't do it for
   them.
6. **Say what's untested.** Much of this hasn't been tried on the church's real Wing, Tios and Dante network yet. When
   something works, say so plainly; when it fails, show the output.
7. Use plain English: the people here run the sound, they aren't IT staff.

## The order (HP Set-up guide)

The person does 1 and 2 themselves before you're installed: **BIOS** (F10: After Power Loss = Power On, S5 Maximum
Power Savings = Disable, S5 Wake on LAN on) and **Windows 10 IoT Enterprise LTSC 2021** with the users **admin**
(Administrator) and **booth** (standard), the PC named **C3LIFE**, booth signed in once.

Then, in an Administrator PowerShell in `C:\Setup\c3life-server`:

3. `Set-ExecutionPolicy -Scope Process Bypass -Force`, then `.\Setup-C3LifeServer.ps1` (it shows a list and asks for
   YES). It gives the HP 192.168.10.10.
4. Install Dante Controller, Dante Virtual Soundcard (ASIO, 64 x 64, 4 ms or more, Ethernet, named C3LIFE: don't
   rename it), R Remote V6 and Companion (GUI on All interfaces, port 8000, Run at login). Then
   `winget install Python.Python.3.12 --scope machine` and `winget install Gyan.FFmpeg --scope machine`.
5. `.\Install-C3Life.ps1` (YES). Then the person runs Sysinternals **Autologon** for booth. Restart: it signs in as
   booth and C3 Life opens full screen.
6. Power: UPS (USB lead to the HP) and the smart plug (reserved at 192.168.10.5, Power-on default On, Cloud off).
7. The Mac mini helper (on the Mac, the person does it) and the switch's DHCP with two reservations: the HP .10 and the
   smart plug .5. Everything else is found by itself.
8. The final checks: `.\Check-C3LifeServer.ps1` (every line OK), then the checklist in Step 8 of the HP Set-up guide.
9. Updates: the person makes a read-only GitHub key (README, Order step 11) and runs `.\Set-GitHubKey.ps1` in an
   Administrator PowerShell in `C:\C3Life`, pasting it themselves. Never ask them for it or type it.

## Their system (so the Dante set-up matches)

- **Behringer Wing** with a 64 x 64 Dante card (`wing-d-2627f4`), the Dante clock leader.
- **3 Yamaha Tio1608-D** stage boxes (`Y001-box-1-...`, `Y002-box-2-...`, and a third). Mics and DIs come in on them;
  the monitors and PA go out on them.
- **C3-LIfe-Multitracks** (MacBook Pro, MultiTracks Playback): about 18 track channels, click and guide.
- **C3-Life-Leeds-Media** (Mac mini, ProPresenter and Spotify): ProPresenter on 17/18, Spotify on 15/16.
- **C3-Life-Foyer-AVIO** (foyer, Foyer L/R from Wing 59/60) and a **MIPRO MI-58** (Sound bus, Wing 55/56).
- **16 stereo monitor outputs**: 3 Shure PSM300 and 5 Behringer P2, fed from Wing buses through the Tios' outputs.
- Many Wing inputs have a **"Mon-" copy** on a second card input (the same Tio channel routed twice) for the monitors.
- The church's patch sheet is `server/seed/patch_sheet.csv` (loaded by C3 Life on first start for mic and stand).

How the sound gets to the HP (Dante and Wing guide, Steps 2 and 3):

- **Recording** takes its sound **straight from the sources** (the Tios, the Multitracks Mac, the Media Mac), the same
  channels the Wing takes: Dante sends one channel to several places. Connect them in the app on **Record > Patch**
  (it asks first), stereo pairs as pairs, then save the patch as the layout **"Sunday"**. About 56 of the HP's 64 channels.
  Don't use the Wing's card outputs for recording.
- **The room mic** (DBX, Wing Local in 1) goes out on a free **Wing card output**: in the app, RTA > Where the RTA
  listens > Wing input, Local 1 > **Set it up for me**.
- **The stream** (Matrix 2 L/R) goes out on two free **Wing card outputs** (Mixer > Patch), into two C3LIFE channels,
  chosen in Listen settings > Sources.
- **FOH L/R** (Wing 61/62) is the RTA's desk reference; **Tio 2 "14 MD"** is the MD's mic for MD talk.
- **Playback** needs no routes: C3 Life swaps the Wing's card inputs to C3LIFE while it plays and puts them back.
- If Dante Controller says a device is full, switch that device's channels to **multicast**.
- When it's done: Internet & reports > **Patch list > Send it now**, and save a Dante Controller preset.

Check the old card outputs 1-45 (labelled as direct outs in their old Dante preset) aren't feeding anything before
reusing one: the patch list shows who receives each card output.

## Useful commands

- `.\Check-C3LifeServer.ps1`: checks everything, reads only.
- `schtasks /end /tn "C3 Life"` and `schtasks /run /tn "C3 Life"`: stop and start C3 Life.
- Logs: `C:\C3Life\logs`. Settings: `C:\C3Life\server\config.json`. Data (PINs hashed, mixes, layouts): `C:\C3Life\data`.
- The app on any phone on the booth Wi-Fi: http://192.168.10.10 (or http://c3life.local) opens Sign in. Listening for
  anyone, no sign-in: http://192.168.10.10/listen (and /listen/<page>). The foyer team: /foyer. Help > Run system check
  reads every device and changes nothing.
- Simple or Full: each person chooses on Home and My mix for themselves (kept on the server). Don't choose it for them.
  Musicians' own mixes are set on Mixer > **Who's on which mix** (FOH or an Admin; it asks first, the Wing doesn't
  change). Every screen has **Help with this screen** at the bottom of its menu.
- Updating later: in the app, Saved setup > Updates (an Admin; it backs up first, installs with the same options and puts
  the backup back if C3 Life doesn't start; never on a Sunday). It needs the GitHub key once (`Set-GitHubKey.ps1`, the
  person pastes it). By hand: the "Updating the app on the HP" section of the manual. Log: `C:\C3Life\logs\update.log`.
