# Setup Checklist: Drone on the ATAK Map

Work through this top to bottom and tick each box as you go. You type the commands
in `grey boxes` into the Raspberry Pi's terminal, exactly as shown, and press **Enter**
after each one.

Expect about an hour the first time. Do everything on a table (the "bench") before you
go anywhere near a field.

---

## Part 1: Get the gear together

- [ ] **Raspberry Pi 4 or 5** (8 GB) with its power supply
- [ ] **microSD card**, 32 GB or bigger
- [ ] A computer to prepare the SD card
- [ ] The drone's **ground telemetry radio**, the small radio with a USB plug that
      normally connects to a laptop
- [ ] **Wi-Fi** that the Pi and all the phones can join (a travel router or a phone hotspot)
- [ ] One or more **phones** for the map: Android uses **ATAK-CIV** (Google Play Store),
      iPhone uses **iTAK** (App Store). Both are free and both work.
- [ ] Know which **autopilot** the drone runs. It must be **ArduPilot (ArduPlane)** or
      **PX4**. If it's DJI, stop here, because this won't work with DJI.

---

## Part 2: Set up the Raspberry Pi

- [ ] On your computer, download **Raspberry Pi Imager** from raspberrypi.com/software
- [ ] Put the SD card in and open Imager. Choose your Pi model, then
      **Raspberry Pi OS (64-bit)**, then your SD card.
- [ ] When it asks about settings, click **Edit Settings** and:
  - [ ] set a username and password (write them down)
  - [ ] enter the Wi-Fi name and password from Part 1
  - [ ] on the Services tab, turn on **SSH** if you want to control the Pi from a laptop
- [ ] Write the card, put it in the Pi, and plug in a screen, keyboard and power
- [ ] Once it boots, open **Terminal** (the black icon at the top)

---

## Part 3: Install the drone software

- [ ] Install git, the tool that downloads code:
  ```
  sudo apt update && sudo apt install -y git
  ```
- [ ] Download the code:
  ```
  git clone -b claude/atak-fixed-wing-drone-h646nk https://github.com/MessyGamer/Hardy.git hardy
  ```
  (If GitHub asks for a login, the repo is private. Sign in with the GitHub account that
  owns it, or ask the owner to make it public or add you.)
- [ ] Go into the folder and run the installer:
  ```
  cd hardy
  sudo ./scripts/install.sh
  ```
  This takes a few minutes. When it finishes, it prints a line like
  `Point ATAK at this Pi: TCP port 8087 on 192.168.x.x`.
- [ ] **Write down that number** (the Pi's address): `________________`

From now on, the software starts by itself every time the Pi is turned on.

---

## Part 4: Test with a pretend plane (no drone needed)

This checks the phones and Pi work before you involve the real aircraft.

- [ ] On the Pi, start the pretend plane:
  ```
  /opt/atak-bridge/venv/bin/python tools/fake_uav.py --lat 35.0 --lon -117.0 --out udpout:127.0.0.1:14551
  ```
  Tip: change `35.0` and `-117.0` to your own location (right-click your house in Google
  Maps to copy the numbers), so the pretend plane circles near you.
- [ ] Leave that running. On the phone, install **ATAK-CIV** (Android, Google Play Store) or
      **iTAK** (iPhone, App Store) and open it. Allow the permissions it asks for.
- [ ] Connect the phone to the **same Wi-Fi** as the Pi
- [ ] Add the Pi as a server:
  - **Android (ATAK):** ☰ menu → Settings → Network Preferences → TAK Servers → Add
  - **iPhone (iTAK):** first set a password: in `/etc/atak-bridge/config.toml`, under
    `[secure.users]`, change `hardy = "change-me"` to your own password, then restart
    (`sudo systemctl restart atak-bridge`). iTAK's own add-server screen won't work here.
    Instead, open **Safari** on the phone, go to `http://<Pi address>:8087`, tap
    the **iPhone (iTAK)** button, and open the file in iTAK (details are on that page).
    This works on Android too.

  Fill in:
  - Description: `Drone`
  - Address: the Pi address you wrote down
  - Port: `8087`, protocol: **TCP**
  - **Untick** "Use default SSL/TLS certificates" (ATAK only)
  - Tap **OK**. A small dot in the corner of the map should turn **green**.
- [ ] You should see an airplane icon called **HARDY-1** flying in circles 🎉
- [ ] Tap the icon and check it shows speed, altitude and battery
- [ ] On the Pi, press **Ctrl + C** to stop the pretend plane

**If the plane doesn't appear,** see Troubleshooting at the bottom.

---

## Part 5: Hook up the real drone's radio

- [ ] Plug the ground telemetry radio into the Pi's USB port
- [ ] Find its name:
  ```
  ls /dev/ttyUSB* /dev/ttyACM*
  ```
  Write down what it shows (usually `/dev/ttyUSB0`): `________________`
- [ ] Open the settings file:
  ```
  sudo nano /etc/atak-bridge/config.toml
  ```
- [ ] Use the arrow keys to find these lines and change them:
  - `connection = "udpin:0.0.0.0:14551"` → `connection = "/dev/ttyUSB0"` (use the name
    you wrote down)
  - `baud = 57600`: leave as is unless the radio was set to a different speed
  - `callsign = "HARDY-1"`: the name shown on the map, change it if you like
- [ ] Save and exit: press **Ctrl + O**, **Enter**, then **Ctrl + X**
- [ ] Restart the software:
  ```
  sudo systemctl restart atak-bridge
  ```

> ⚠️ **Heads-up:** only one program can use the radio at once. If you also want to watch
> the drone in **Mission Planner** or **QGroundControl** on a laptop using this same radio,
> skip this part and follow "Option A" in `README.md` instead. It shares the radio between
> both.

---

## Part 6: Test with the real drone (on the bench, propeller OFF)

- [ ] **Take the propeller off** or make sure the motor can't spin
- [ ] Power up the drone and wait about a minute for GPS (works best outdoors or near a window)
- [ ] Watch the software's log on the Pi:
  ```
  journalctl -u atak-bridge -f
  ```
  You want to see **"Locked onto vehicle"** and **"heartbeat acquired"**.
  (Press **Ctrl + C** to stop watching. The software keeps running.)
- [ ] The drone appears on the ATAK map at its real location
- [ ] A **HOME** pin appears once the drone has a GPS fix

---

## Part 7: Before every flight

- [ ] Pi powered on and connected to Wi-Fi
- [ ] Phones connected (green dot in ATAK)
- [ ] Drone icon visible and in the right place
- [ ] The pilot has the **normal remote control** in hand. The map is for watching; the
      remote is for flying.
- [ ] You're following your local drone rules (in the US: FAA registration, stay under
      400 ft, keep the plane in sight)

---

## Leave OFF unless you know exactly why you need it

The settings file has a **`[commands]`** section with `enabled = false`. Turning it on lets
people on the map send the plane somewhere by dropping a pin named `GOTO`. **Leave it off.**
If you want it later, read the "Safety" section of `README.md` first.

---

## Troubleshooting

| Problem | Try this |
|---|---|
| ATAK dot stays red | Phone and Pi on the same Wi-Fi? Address typed right? SSL box unticked? Re-check the Pi's address with `hostname -I` |
| Connected, but no plane | Look at the log: `journalctl -u atak-bridge -f`. If it never says "heartbeat acquired", the radio isn't talking to the Pi. |
| "Permission denied" on the radio | Restart the Pi: `sudo reboot` |
| No `/dev/ttyUSB0` | Unplug and replug the radio, try another USB port, run the `ls` command again |
| Drone connects, but the plane is at the wrong spot or missing | The drone needs a GPS fix. Go outside and wait a couple of minutes. |
| Still stuck | Send the output of `journalctl -u atak-bridge -n 100` to whoever is helping you |

**Useful commands**

| What | Command |
|---|---|
| Watch what it's doing | `journalctl -u atak-bridge -f` |
| Restart it | `sudo systemctl restart atak-bridge` |
| Stop it | `sudo systemctl stop atak-bridge` |
| Is it running? | `systemctl status atak-bridge` |
| Pi's address | `hostname -I` |
