# NullscapeDiscordRPC
A program that opens [Nullscape](https://www.roblox.com/games/129279692364812/Nullscape) and shows it as your rich presence activity on Discord instead of Roblox.

Fully made using Claude because I was honestly really lazy and wanted to get this done quickly. Sue me.

## What why?
So basically, this project started from me wanting to do free marketing for Nullscape on Discord, since it is an amazing game that needs to be recognized as more than just "Playing Roblox" (which is not a game btw...). It also made things a bit more convenient for me as it automatically launched Roblox and put me right into Nullscape.

After the Delusional Descension update for Nullscape was released, I realized the program (originally just a single Python script) was lacking automatic icon updates. I decided it was best for myself and everyone who might be interested in using this program if I made it so user action is never required. You would install it once and it would just work, like any other complete piece of software would. And so, I ended up doing exactly that!

On top of being basically UX heaven with zero user interaction after its one-click install, the program is also designed for maximum portability. It only uses one directory for everything, meaning you can even put it onto a USB stick and run it on any PC without leaving a single trace of its existence on said PC, even though it has automatic icon updates now. Very cool.

Anyway. At the end of the day this project is really not that special. Simply put, it's an overengineered script for adding Discord RPC to Nullscape. I just really enjoyed writing about it because I think it came out pretty cool. And because I'm autistic. Meow! :3

## Usage
### Windows
1. Download the installer or portable version from the [latest release](https://github.com/Kamppix/NullscapeDiscordRPC/releases/latest).
2. Run the installer, configure settings according to your preferences, and click "Install" OR extract the portable zip file into your desired installation folder.
3. Done! You can now start the program via a "Nullscape" shortcut or directly from the installation folder.

### Linux
Requires a reasonably recent distro (glibc 2.36 or newer: e.g. Ubuntu 23.04, Debian 12, Fedora 37). Uses [Sober](https://sober.vinegarhq.org) to run Roblox, so install that first if you haven't already.
1. Download the `.AppImage` file from the [latest release](https://github.com/Kamppix/NullscapeDiscordRPC/releases/latest).
2. Make the file executable with `chmod +x NullscapeDiscordRPC-*.AppImage`, or through your file manager's file properties.
3. Done! You can now start the program.
4. (optional) Run the program once with `--integrate` to add "Nullscape" to your applications menu. A menu entry will be added to `~/.local/share/applications/` and an (auto-updating) icon file to `~/.local/share/icons/`. To undo integration, simply delete those two files.

### Options
Run the program with `--log` to write an activity log to a file in the current directory. The Windows installer and uninstaller have this option too. On Linux, the log is always printed to the terminal.
