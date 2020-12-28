#!/usr/bin/env python3
# encoding:utf8
"""Read and copy Keepass database entries using dmenu or rofi

"""
import configparser
import argparse
import logging

from functools import partial
from contextlib import closing
from enum import Enum
import errno
import re
import itertools
import locale
from multiprocessing import Event, Process, Queue
from multiprocessing.managers import BaseManager
import os
from os.path import exists, expanduser
import random
import shlex
import socket
import string
import sys
from subprocess import call, Popen, PIPE
import tempfile
from threading import Timer
import time
import re
import webbrowser
import construct
from pynput import keyboard
from pykeepass import PyKeePass

LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

try:
    # secrets only available python 3.6+
    from secrets import choice
except ImportError:
    def choice(seq):
        """Provide `choice` function call for pw generation

        """
        return random.SystemRandom().choice(seq)


AUTH_FILE = expanduser("~/.cache/.keepmenu-auth")
CONF_FILE = expanduser("~/.config/keepmenu/config.ini")

class MenuOption(Enum):
    ViewEntry = 0
    Edit = 1
    Add = 2
    ManageGroups = 3
    ReloadDB = 4
    KillDaemon = 5
    TypePassword = 6
    TypeEntry = 7
    TypeUsername = 8

    def description(self):
        return {
            self.TypePassword:'Type password',
            self.ViewEntry:'View Individual entry',
            self.Edit:'Edit entries',
            self.Add:'Add entry',
            self.ManageGroups:'Manage groups',
            self.TypeUsername:'Type username',
            self.ReloadDB:'Reload database',
            self.KillDaemon:'Kill Keepmenu daemon',
            self.TypeEntry:'Select entry to autotype',
        }.get(self)

def find_free_port():
    """Find random free port to use for BaseManager server

    Returns: int Port

    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(('127.0.0.1', 0))  # pylint:disable=no-member
        return sock.getsockname()[1]  # pylint:disable=no-member


def random_str():
    """Generate random auth string for BaseManager

    Returns: string

    """
    letters = string.ascii_lowercase
    return ''.join(random.choice(letters) for i in range(15))


def gen_passwd(chars, length=20):
    """Generate password (min = # of distinct character sets picked)

    Args: chars - Dict {preset_name_1: {char_set_1: string, char_set_2: string},
                        preset_name_2: ....}
          length - int (default 20)

    Returns: password - string OR False

    """
    sets = set()
    if chars:
        sets = set(j for i in chars.values() for j in i.values())
    if length < len(sets) or not chars:
        return False
    alphabet = "".join(set("".join(j for j in i.values()) for i in chars.values()))
    # Ensure minimum of one char from each character set
    password = "".join(choice(k) for k in sets)
    password += "".join(choice(alphabet) for i in range(length - len(sets)))
    tpw = list(password)
    random.shuffle(tpw)
    return "".join(tpw)


def process_config():
    """Set global variables. Read the config file. Create default config file if
    one doesn't exist.

    """
    # pragma pylint: disable=global-variable-undefined
    global CACHE_PERIOD_MIN, \
        CACHE_PERIOD_DEFAULT_MIN, \
        CONF, \
        DMENU_LEN, \
        ENV, \
        ENC, \
        SEQUENCE
    # pragma pylint: enable=global-variable-undefined
    ENV = os.environ.copy()
    ENV['LC_ALL'] = 'C'
    ENC = locale.getpreferredencoding()
    CACHE_PERIOD_DEFAULT_MIN = 360
    SEQUENCE = "{USERNAME}{TAB}{PASSWORD}{ENTER}"
    CONF = configparser.ConfigParser()
    if not exists(CONF_FILE):
        try:
            os.mkdir(os.path.dirname(CONF_FILE))
        except OSError:
            pass
        with open(CONF_FILE, 'w') as conf_file:
            CONF.add_section('dmenu')
            CONF.set('dmenu', 'dmenu_command', 'dmenu')
            CONF.add_section('dmenu_passphrase')
            CONF.set('dmenu_passphrase', 'nf', '#222222')
            CONF.set('dmenu_passphrase', 'nb', '#222222')
            CONF.set('dmenu_passphrase', 'rofi_obscure', 'True')
            CONF.add_section('database')
            CONF.set('database', 'database_1', '')
            CONF.set('database', 'keyfile_1', '')
            CONF.set('database', 'pw_cache_period_min', str(CACHE_PERIOD_DEFAULT_MIN))
            CONF.set('database', 'autotype_default', SEQUENCE)
            CONF.write(conf_file)
    try:
        CONF.read(CONF_FILE)
    except configparser.ParsingError as err:
        dmenu_err("Config file error: {}".format(err))
        sys.exit()
    if CONF.has_option("database", "pw_cache_period_min"):
        CACHE_PERIOD_MIN = int(CONF.get("database", "pw_cache_period_min"))
    else:
        CACHE_PERIOD_MIN = CACHE_PERIOD_DEFAULT_MIN
    if CONF.has_option("dmenu", "l"):
        DMENU_LEN = int(CONF.get("dmenu", "l"))
    else:
        DMENU_LEN = 24
    if CONF.has_option('database', 'autotype_default'):
        SEQUENCE = CONF.get("database", "autotype_default")
    if CONF.has_option("database", "type_library"):
        if CONF.get("database", "type_library") == "xdotool":
            try:
                call(['xdotool', 'version'])
            except OSError:
                dmenu_err("Xdotool not installed.\n"
                          "Please install or remove that option from config.ini")
                sys.exit()
        elif CONF.get("database", "type_library") == "ydotool":
            try:
                call(['ydotool'])
            except OSError:
                dmenu_err("Ydotool not installed.\n"
                          "Please install or remove that option from config.ini")
                sys.exit()


def get_auth():
    """Generate and save port and authkey to ~/.cache/.keepmenu-auth

    Returns: int port, bytestring authkey

    """
    auth = configparser.ConfigParser()
    if not exists(AUTH_FILE):
        fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
        with open(fd, 'w') as a_file:
            auth.set('DEFAULT', 'port', str(find_free_port()))
            auth.set('DEFAULT', 'authkey', random_str())
            auth.write(a_file)
    try:
        auth.read(AUTH_FILE)
        port = auth.get('DEFAULT', 'port')
        authkey = auth.get('DEFAULT', 'authkey').encode()
    except (configparser.NoOptionError, configparser.MissingSectionHeaderError):
        os.remove(AUTH_FILE)
        print("Cache file was corrupted. Stopping all instances. Please try again")
        call(["pkill", "keepmenu"])  # Kill all prior instances as well
        return None, None
    return int(port), authkey


def dmenu_cmd(num_lines, prompt):
    """Parse config.ini for dmenu options

    Args: args - num_lines: number of lines to display
                 prompt: prompt to show
    Returns: command invocation (as a list of strings) for
                dmenu -l <num_lines> -p <prompt> -i ...

    """
    args_dict = {"dmenu_command": "dmenu"}
    if CONF.has_section('dmenu'):
        args = CONF.items('dmenu')
        args_dict.update(dict(args))
    command = shlex.split(args_dict["dmenu_command"])
    dmenu_command = command[0]
    dmenu_args = command[1:]
    del args_dict["dmenu_command"]
    lines = "-i -dmenu -multi-select -lines" if "rofi" in dmenu_command else "-i -l"
    if "l" in args_dict:
        lines = "{} {}".format(lines, min(num_lines, int(args_dict['l'])))
        del args_dict['l']
    else:
        lines = "{} {}".format(lines, num_lines)
    if "pinentry" in args_dict:
        del args_dict["pinentry"]
    if prompt == "Passphrase":
        if CONF.has_section('dmenu_passphrase'):
            args = CONF.items('dmenu_passphrase')
            args_dict.update(args)
        rofi_obscure = True
        if CONF.has_option('dmenu_passphrase', 'rofi_obscure'):
            rofi_obscure = CONF.getboolean('dmenu_passphrase', 'rofi_obscure')
            del args_dict["rofi_obscure"]
        if rofi_obscure is True and "rofi" in dmenu_command:
            dmenu_args.extend(["-password"])
    extras = (["-" + str(k), str(v)] for (k, v) in args_dict.items())
    dmenu = [dmenu_command, "-p", str(prompt)]
    dmenu.extend(dmenu_args)
    dmenu += list(itertools.chain.from_iterable(extras))
    dmenu[1:1] = lines.split()
    dmenu = list(filter(None, dmenu))  # Remove empty list elements
    return dmenu


def dmenu_select(num_lines, prompt="Entries", inp=""):
    """Call dmenu and return the selected entry

    Args: num_lines - number of lines to display
          prompt - prompt to show
          inp - bytes string to pass to dmenu via STDIN

    Returns: sel - string

    """
    cmd = dmenu_cmd(num_lines, prompt)
    sel, err = Popen(cmd,
                     stdin=PIPE,
                     stdout=PIPE,
                     stderr=PIPE,
                     env=ENV).communicate(input=inp)
    if err:
        cmd = [cmd[0]] + ["-dmenu"] if "rofi" in cmd[0] else [""]
        Popen(cmd[0], stdin=PIPE, stdout=PIPE, env=ENV).communicate(input=err)
        sys.exit()
    if sel is not None:
        sel = sel.decode(ENC).rstrip('\n')
    return sel


def dmenu_err(prompt):
    """Pops up a dmenu prompt with an error message

    """
    return dmenu_select(1, prompt)


def get_password_chars():
    """Get characters to use for password generation from defaults, config file
    and user input.

    Returns: Dict {preset_name_1: {char_set_1: string, char_set_2: string},
                   preset_name_2: ....}
    """
    chars = {"upper": string.ascii_uppercase,
             "lower": string.ascii_lowercase,
             "digits": string.digits,
             "punctuation": string.punctuation}
    presets = {}
    presets["Letters+Digits+Punctuation"] = chars
    presets["Letters+Digits"] = {k: chars[k] for k in ("upper", "lower", "digits")}
    presets["Letters"] = {k: chars[k] for k in ("upper", "lower")}
    presets["Digits"] = {k: chars[k] for k in ("digits",)}
    if CONF.has_section('password_chars'):
        pw_chars = dict(CONF.items('password_chars'))
        chars.update(pw_chars)
        for key, val in pw_chars.items():
            presets[key.title()] = {k: chars[k] for k in (key,)}
    if CONF.has_section('password_char_presets'):
        if CONF.options('password_char_presets'):
            presets = {}
        for name, val in CONF.items('password_char_presets'):
            try:
                presets[name.title()] = {k: chars[k] for k in shlex.split(val)}
            except KeyError:
                print("Error: Unknown value in preset {}. Ignoring.".format(name))
                continue
    input_b = "\n".join(presets).encode(ENC)
    char_sel = dmenu_select(len(presets),
                            "Pick character set(s) to use", inp=input_b)
    # This dictionary return also handles Rofi multiple select
    return {k: presets[k] for k in char_sel.split('\n')} if char_sel else False


def get_database():
    """Read databases from config or ask for user input.

    Returns: (database name, keyfile, passphrase)
             Returns (None, None, None) on error selecting database

    """
    args = CONF.items('database')
    args_dict = dict(args)
    dbases = [i for i in args_dict if i.startswith('database')]
    dbs = []
    for dbase in dbases:
        dbn = expanduser(args_dict[dbase])
        idx = dbase.rsplit('_', 1)[-1]
        try:
            keyfile = expanduser(args_dict['keyfile_{}'.format(idx)])
        except KeyError:
            keyfile = ''
        try:
            passw = args_dict['password_{}'.format(idx)]
        except KeyError:
            passw = ''
        try:
            cmd = args_dict['password_cmd_{}'.format(idx)]
            res = Popen(shlex.split(cmd), stdout=PIPE, stderr=PIPE).communicate()
            if res[1]:
                dmenu_err("Password command error: {}".format(res[1]))
                sys.exit()
            else:
                passw = res[0].decode().rstrip('\n') if res[0] else passw
        except KeyError:
            pass
        if dbn:
            dbs.append((dbn, keyfile, passw))
    if not dbs:
        res = get_initial_db()
        if res is True:
            dbs = [get_database()]
        else:
            return (None, None, None)
    if len(dbs) > 1:
        inp_bytes = "\n".join(i[0] for i in dbs).encode(ENC)
        sel = dmenu_select(len(dbs), "Select Database", inp=inp_bytes)
        dbs = [i for i in dbs if i[0] == sel]
        if not sel or not dbs:
            return (None, None, None)
    if not dbs[0][-1]:
        db_l = list(dbs[0])
        db_l[-1] = get_passphrase()
        dbs[0] = db_l
    return dbs[0]


def get_initial_db():
    """Ask for initial database name and keyfile if not entered in config file

    """
    db_name = dmenu_select(0, "Enter path to existing "
                              "Keepass database. ~/ for $HOME is ok")
    if not db_name:
        dmenu_err("No database entered. Try again.")
        return False
    keyfile_name = dmenu_select(0, "Enter path to keyfile. ~/ for $HOME is ok")
    with open(CONF_FILE, 'w') as conf_file:
        CONF.set('database', 'database_1', db_name)
        if keyfile_name:
            CONF.set('database', 'keyfile_1', keyfile_name)
        CONF.write(conf_file)
    return True


def get_entries(dbo):
    """Open keepass database and return the PyKeePass object

        Args: dbo: tuple (db path, keyfile path, password)
        Returns: PyKeePass object

    """
    dbf, keyfile, password = dbo
    if dbf is None:
        return None
    try:
        kpo = PyKeePass(dbf, password, keyfile=keyfile)
    except (FileNotFoundError, construct.core.ChecksumError) as err:
        if str(err.args[0]).startswith("wrong checksum"):
            dmenu_err("Invalid Password or keyfile")
            return None
        try:
            if err.errno == errno.ENOENT:
                if not os.path.isfile(dbf):
                    dmenu_err("Database does not exist. Edit ~/.config/keepmenu/config.ini")
                elif not os.path.isfile(keyfile):
                    dmenu_err("Keyfile does not exist. Edit ~/.config/keepmenu/config.ini")
        except AttributeError:
            pass
        return None
    except Exception as err:
        dmenu_err("Error: {}".format(err))
        return None
    return kpo


def get_passphrase():
    """Get a database password from dmenu or pinentry

    Returns: string

    """
    pinentry = None
    if CONF.has_option("dmenu", "pinentry"):
        pinentry = CONF.get("dmenu", "pinentry")
    if pinentry:
        password = ""
        out = Popen(pinentry,
                    stdout=PIPE,
                    stdin=PIPE).communicate(
                        input=b'setdesc Enter database password\ngetpin\n')[0]
        if out:
            res = out.decode(ENC).split("\n")[2]
            if res.startswith("D "):
                password = res.split("D ")[1]
    else:
        password = dmenu_select(0, "Passphrase")
        if not password:
            sys.exit()
    return password


def tokenize_autotype(autotype):
    """Process the autotype sequence

    Args: autotype - string
    Returns: tokens - generator ((token, if_special_char T/F), ...)

    """
    while autotype:
        opening_idx = -1
        for char in "{+^%~@":
            idx = autotype.find(char)
            if idx != -1 and (opening_idx == -1 or idx < opening_idx):
                opening_idx = idx

        if opening_idx == -1:
            # found the end of the string without further opening braces or
            # other characters
            yield autotype, False
            return

        if opening_idx > 0:
            yield autotype[:opening_idx], False

        if autotype[opening_idx] in "+^%~@":
            yield autotype[opening_idx], True
            autotype = autotype[opening_idx + 1:]
            continue

        closing_idx = autotype.find('}')
        if closing_idx == -1:
            dmenu_err("Unable to find matching right brace (}) while" +
                      "tokenizing auto-type string: %s\n" % (autotype))
            return
        if closing_idx == opening_idx + 1 and closing_idx + 1 < len(autotype) \
                and autotype[closing_idx + 1] == '}':
            yield "{}}", True
            autotype = autotype[closing_idx + 2:]
            continue
        yield autotype[opening_idx:closing_idx + 1], True
        autotype = autotype[closing_idx + 1:]


def token_command(token):
    """When token denotes a special command, this function provides a callable
    implementing its behaviour.

    """
    cmd = None

    def _check_delay():
        match = re.match(r'{DELAY (\d+)}', token)
        if match:
            delay = match.group(1)
            nonlocal cmd
            cmd = lambda t=delay: time.sleep(int(t) / 1000)
            return True
        return False

    if _check_delay():  # {DELAY x}
        return cmd
    return None


def type_entry(entry):
    """Pick which library to use to type strings

    Defaults to pynput

    """
    sequence = SEQUENCE
    if hasattr(entry, 'autotype_enabled') and entry.autotype_enabled is False:
        dmenu_err("Autotype disabled for this entry")
        return
    if hasattr(entry, 'autotype_sequence') and \
            entry.autotype_sequence is not None and \
            entry.autotype_sequence != 'None':
        sequence = entry.autotype_sequence
    tokens = tokenize_autotype(sequence)

    library = 'pynput'
    if CONF.has_option('database', 'type_library'):
        library = CONF.get('database', 'type_library')
    if library == 'xdotool':
        type_entry_xdotool(entry, tokens)
    elif library == 'ydotool':
        type_entry_ydotool(entry, tokens)
    else:
        type_entry_pynput(entry, tokens)


PLACEHOLDER_AUTOTYPE_TOKENS = {
    "{TITLE}"   : lambda e: e.title,
    "{USERNAME}": lambda e: e.username,
    "{URL}"     : lambda e: e.url,
    "{PASSWORD}": lambda e: e.password,
    "{NOTES}"   : lambda e: e.notes,
}

STRING_AUTOTYPE_TOKENS = {
    "{PLUS}"      : '+',
    "{PERCENT}"   : '%',
    "{CARET}"     : '^',
    "{TILDE}"     : '~',
    "{LEFTPAREN}" : '(',
    "{RIGHTPAREN}": ')',
    "{LEFTBRACE}" : '{',
    "{RIGHTBRACE}": '}',
    "{AT}"        : '@',
    "{+}"         : '+',
    "{%}"         : '%',
    "{^}"         : '^',
    "{~}"         : '~',
    "{(}"         : '(',
    "{)}"         : ')',
    "{[}"         : '[',
    "{]}"         : ']',
    "{{}"         : '{',
    "{}}"         : '}',
}

PYNPUT_AUTOTYPE_TOKENS = {
    "{TAB}"       : keyboard.Key.tab,
    "{ENTER}"     : keyboard.Key.enter,
    "~"           : keyboard.Key.enter,
    "{UP}"        : keyboard.Key.up,
    "{DOWN}"      : keyboard.Key.down,
    "{LEFT}"      : keyboard.Key.left,
    "{RIGHT}"     : keyboard.Key.right,
    "{INSERT}"    : keyboard.Key.insert,
    "{INS}"       : keyboard.Key.insert,
    "{DELETE}"    : keyboard.Key.delete,
    "{DEL}"       : keyboard.Key.delete,
    "{HOME}"      : keyboard.Key.home,
    "{END}"       : keyboard.Key.end,
    "{PGUP}"      : keyboard.Key.page_up,
    "{PGDN}"      : keyboard.Key.page_down,
    "{SPACE}"     : keyboard.Key.space,
    "{BACKSPACE}" : keyboard.Key.backspace,
    "{BS}"        : keyboard.Key.backspace,
    "{BKSP}"      : keyboard.Key.backspace,
    "{BREAK}"     : keyboard.Key.pause,
    "{CAPSLOCK}"  : keyboard.Key.caps_lock,
    "{ESC}"       : keyboard.Key.esc,
    "{WIN}"       : keyboard.Key.cmd,
    "{LWIN}"      : keyboard.Key.cmd_l,
    "{RWIN}"      : keyboard.Key.cmd_r,
    # "{APPS}"    : keyboard.Key.
    # "{HELP}"    : keyboard.Key.
    "{NUMLOCK}"   : keyboard.Key.num_lock,
    "{PRTSC}"     : keyboard.Key.print_screen,
    "{SCROLLLOCK}": keyboard.Key.scroll_lock,
    "{F1}"        : keyboard.Key.f1,
    "{F2}"        : keyboard.Key.f2,
    "{F3}"        : keyboard.Key.f3,
    "{F4}"        : keyboard.Key.f4,
    "{F5}"        : keyboard.Key.f5,
    "{F6}"        : keyboard.Key.f6,
    "{F7}"        : keyboard.Key.f7,
    "{F8}"        : keyboard.Key.f8,
    "{F9}"        : keyboard.Key.f9,
    "{F10}"       : keyboard.Key.f10,
    "{F11}"       : keyboard.Key.f11,
    "{F12}"       : keyboard.Key.f12,
    "{F13}"       : keyboard.Key.f13,
    "{F14}"       : keyboard.Key.f14,
    "{F15}"       : keyboard.Key.f15,
    "{F16}"       : keyboard.Key.f16,
    # "{ADD}"       : keyboard.Key.
    # "{SUBTRACT}"  : keyboard.Key.
    # "{MULTIPLY}"  : keyboard.Key.
    # "{DIVIDE}"    : keyboard.Key.
    # "{NUMPAD0}"   : keyboard.Key.
    # "{NUMPAD1}"   : keyboard.Key.
    # "{NUMPAD2}"   : keyboard.Key.
    # "{NUMPAD3}"   : keyboard.Key.
    # "{NUMPAD4}"   : keyboard.Key.
    # "{NUMPAD5}"   : keyboard.Key.
    # "{NUMPAD6}"   : keyboard.Key.
    # "{NUMPAD7}"   : keyboard.Key.
    # "{NUMPAD8}"   : keyboard.Key.
    # "{NUMPAD9}"   : keyboard.Key.
    "+"           : keyboard.Key.shift,
    "^"           : keyboard.Key.ctrl,
    "%"           : keyboard.Key.alt,
    "@"           : keyboard.Key.cmd,
}


def type_entry_pynput(entry, tokens):
    """Use pynput to auto-type the selected entry

    """
    kbd = keyboard.Controller()
    enter_idx = True
    for token, special in tokens:
        if special:
            cmd = token_command(token)
            if callable(cmd):
                cmd()
            elif token in PLACEHOLDER_AUTOTYPE_TOKENS:
                to_type = PLACEHOLDER_AUTOTYPE_TOKENS[token](entry)
                if to_type:
                    try:
                        kbd.type(to_type)
                    except kbd.InvalidCharacterException:
                        dmenu_err("Unable to type string...bad character.\n"
                                  "Try setting `type_library = xdotool` in config.ini")
                        return
            elif token in STRING_AUTOTYPE_TOKENS:
                to_type = STRING_AUTOTYPE_TOKENS[token]
                try:
                    kbd.type(to_type)
                except kbd.InvalidCharacterException:
                    dmenu_err("Unable to type string...bad character.\n"
                              "Try setting `type_library = xdotool` in config.ini")
                    return
            elif token in PYNPUT_AUTOTYPE_TOKENS:
                to_tap = PYNPUT_AUTOTYPE_TOKENS[token]
                kbd.tap(to_tap)
                # Add extra {ENTER} key tap for first instance of {ENTER}. It
                # doesn't get recognized for some reason.
                if enter_idx is True and token in ("{ENTER}", "~"):
                    kbd.tap(to_tap)
                    enter_idx = False
            else:
                dmenu_err("Unsupported auto-type token (pynput): \"%s\"" % (token))
                return
        else:
            try:
                kbd.type(token)
            except kbd.InvalidCharacterException:
                dmenu_err("Unable to type string...bad character.\n"
                          "Try setting `type_library = xdotool` in config.ini")
                return


XDOTOOL_AUTOTYPE_TOKENS = {
    "{TAB}"       : ['key', 'Tab'],
    "{ENTER}"     : ['key', 'Return'],
    "~"           : ['key', 'Return'],
    "{UP}"        : ['key', 'Up'],
    "{DOWN}"      : ['key', 'Down'],
    "{LEFT}"      : ['key', 'Left'],
    "{RIGHT}"     : ['key', 'Right'],
    "{INSERT}"    : ['key', 'Insert'],
    "{INS}"       : ['key', 'Insert'],
    "{DELETE}"    : ['key', 'Delete'],
    "{DEL}"       : ['key', 'Delete'],
    "{HOME}"      : ['key', 'Home'],
    "{END}"       : ['key', 'End'],
    "{PGUP}"      : ['key', 'Page_Up'],
    "{PGDN}"      : ['key', 'Page_Down'],
    "{SPACE}"     : ['type', ' '],
    "{BACKSPACE}" : ['key', 'BackSpace'],
    "{BS}"        : ['key', 'BackSpace'],
    "{BKSP}"      : ['key', 'BackSpace'],
    "{BREAK}"     : ['key', 'Break'],
    "{CAPSLOCK}"  : ['key', 'Caps_Lock'],
    "{ESC}"       : ['key', 'Escape'],
    "{WIN}"       : ['key', 'Super'],
    "{LWIN}"      : ['key', 'Super_L'],
    "{RWIN}"      : ['key', 'Super_R'],
    # "{APPS}"      : ['key', ''],
    # "{HELP}"      : ['key', ''],
    "{NUMLOCK}"   : ['key', 'Num_Lock'],
    # "{PRTSC}"     : ['key', ''],
    "{SCROLLLOCK}": ['key', 'Scroll_Lock'],
    "{F1}"        : ['key', 'F1'],
    "{F2}"        : ['key', 'F2'],
    "{F3}"        : ['key', 'F3'],
    "{F4}"        : ['key', 'F4'],
    "{F5}"        : ['key', 'F5'],
    "{F6}"        : ['key', 'F6'],
    "{F7}"        : ['key', 'F7'],
    "{F8}"        : ['key', 'F8'],
    "{F9}"        : ['key', 'F9'],
    "{F10}"       : ['key', 'F10'],
    "{F11}"       : ['key', 'F11'],
    "{F12}"       : ['key', 'F12'],
    "{F13}"       : ['key', 'F13'],
    "{F14}"       : ['key', 'F14'],
    "{F15}"       : ['key', 'F15'],
    "{F16}"       : ['key', 'F16'],
    "{ADD}"       : ['key', 'KP_Add'],
    "{SUBTRACT}"  : ['key', 'KP_Subtract'],
    "{MULTIPLY}"  : ['key', 'KP_Multiply'],
    "{DIVIDE}"    : ['key', 'KP_Divide'],
    "{NUMPAD0}"   : ['key', 'KP_0'],
    "{NUMPAD1}"   : ['key', 'KP_1'],
    "{NUMPAD2}"   : ['key', 'KP_2'],
    "{NUMPAD3}"   : ['key', 'KP_3'],
    "{NUMPAD4}"   : ['key', 'KP_4'],
    "{NUMPAD5}"   : ['key', 'KP_5'],
    "{NUMPAD6}"   : ['key', 'KP_6'],
    "{NUMPAD7}"   : ['key', 'KP_7'],
    "{NUMPAD8}"   : ['key', 'KP_8'],
    "{NUMPAD9}"   : ['key', 'KP_9'],
    "+"           : ['key', 'Shift'],
    "^"           : ['Key', 'Ctrl'],
    "%"           : ['key', 'Alt'],
    "@"           : ['key', 'Super'],
}


def type_entry_xdotool(entry, tokens):
    """Auto-type entry entry using xdotool

    """
    enter_idx = True
    for token, special in tokens:
        if special:
            cmd = token_command(token)
            if callable(cmd):
                cmd()
            elif token in PLACEHOLDER_AUTOTYPE_TOKENS:
                to_type = PLACEHOLDER_AUTOTYPE_TOKENS[token](entry)
                if to_type:
                    call(['xdotool', 'type', to_type])
            elif token in STRING_AUTOTYPE_TOKENS:
                to_type = STRING_AUTOTYPE_TOKENS[token]
                call(['xdotool', 'type', to_type])
            elif token in XDOTOOL_AUTOTYPE_TOKENS:
                cmd = ['xdotool'] + XDOTOOL_AUTOTYPE_TOKENS[token]
                call(cmd)
                # Add extra {ENTER} key tap for first instance of {ENTER}. It
                # doesn't get recognized for some reason.
                if enter_idx is True and token in ("{ENTER}", "~"):
                    cmd = ['xdotool'] + XDOTOOL_AUTOTYPE_TOKENS[token]
                    call(cmd)
                    enter_idx = False
            else:
                dmenu_err("Unsupported auto-type token (xdotool): \"%s\"" % (token))
                return
        else:
            call(['xdotool', 'type', token])


YDOTOOL_AUTOTYPE_TOKENS = {
    "{TAB}"       : ['key', 'TAB'],
    "{ENTER}"     : ['key', 'ENTER'],
    "~"           : ['key', 'Return'],
    "{UP}"        : ['key', 'UP'],
    "{DOWN}"      : ['key', 'DOWN'],
    "{LEFT}"      : ['key', 'LEFT'],
    "{RIGHT}"     : ['key', 'RIGHT'],
    "{INSERT}"    : ['key', 'INSERT'],
    "{INS}"       : ['key', 'INSERT'],
    "{DELETE}"    : ['key', 'DELETE'],
    "{DEL}"       : ['key', 'DELETE'],
    "{HOME}"      : ['key', 'HOME'],
    "{END}"       : ['key', 'END'],
    "{PGUP}"      : ['key', 'PAGEUP'],
    "{PGDN}"      : ['key', 'PAGEDOWN'],
    "{SPACE}"     : ['type', ' '],
    "{BACKSPACE}" : ['key', 'BACKSPACE'],
    "{BS}"        : ['key', 'BACKSPACE'],
    "{BKSP}"      : ['key', 'BACKSPACE'],
    "{BREAK}"     : ['key', 'BREAK'],
    "{CAPSLOCK}"  : ['key', 'CAPSLOCK'],
    "{ESC}"       : ['key', 'ESC'],
    # "{WIN}"       : ['key', 'Super'],
    # "{LWIN}"      : ['key', 'Super_L'],
    # "{RWIN}"      : ['key', 'Super_R'],
    # "{APPS}"      : ['key', ''],
    # "{HELP}"      : ['key', ''],
    "{NUMLOCK}"   : ['key', 'NUMLOCK'],
    # "{PRTSC}"     : ['key', ''],
    "{SCROLLLOCK}": ['key', 'SCROLLLOCK'],
    "{F1}"        : ['key', 'F1'],
    "{F2}"        : ['key', 'F2'],
    "{F3}"        : ['key', 'F3'],
    "{F4}"        : ['key', 'F4'],
    "{F5}"        : ['key', 'F5'],
    "{F6}"        : ['key', 'F6'],
    "{F7}"        : ['key', 'F7'],
    "{F8}"        : ['key', 'F8'],
    "{F9}"        : ['key', 'F9'],
    "{F10}"       : ['key', 'F10'],
    "{F11}"       : ['key', 'F11'],
    "{F12}"       : ['key', 'F12'],
    "{F13}"       : ['key', 'F13'],
    "{F14}"       : ['key', 'F14'],
    "{F15}"       : ['key', 'F15'],
    "{F16}"       : ['key', 'F16'],
    "{ADD}"       : ['key', 'KPPLUS'],
    "{SUBTRACT}"  : ['key', 'KPMINUS'],
    "{MULTIPLY}"  : ['key', 'KPASTERISK'],
    "{DIVIDE}"    : ['key', 'KPSLASH'],
    "{NUMPAD0}"   : ['key', 'KP0'],
    "{NUMPAD1}"   : ['key', 'KP1'],
    "{NUMPAD2}"   : ['key', 'KP2'],
    "{NUMPAD3}"   : ['key', 'KP3'],
    "{NUMPAD4}"   : ['key', 'KP4'],
    "{NUMPAD5}"   : ['key', 'KP5'],
    "{NUMPAD6}"   : ['key', 'KP6'],
    "{NUMPAD7}"   : ['key', 'KP7'],
    "{NUMPAD8}"   : ['key', 'KP8'],
    "{NUMPAD9}"   : ['key', 'KP9'],
    "+"           : ['key', 'LEFTSHIFT'],
    "^"           : ['Key', 'LEFTCTRL'],
    "%"           : ['key', 'LEFTALT'],
    # "@"           : ['key', 'Super']
}


def type_entry_ydotool(entry, tokens):
    """Auto-type entry entry using ydotool

    """
    enter_idx = True
    for token, special in tokens:
        if special:
            cmd = token_command(token)
            if callable(cmd):
                cmd()
            elif token in PLACEHOLDER_AUTOTYPE_TOKENS:
                to_type = PLACEHOLDER_AUTOTYPE_TOKENS[token](entry)
                if to_type:
                    call(['ydotool', 'type', to_type])
            elif token in STRING_AUTOTYPE_TOKENS:
                to_type = STRING_AUTOTYPE_TOKENS[token]
                call(['ydotool', 'type', to_type])
            elif token in YDOTOOL_AUTOTYPE_TOKENS:
                cmd = ['ydotool'] + YDOTOOL_AUTOTYPE_TOKENS[token]
                call(cmd)
                # Add extra {ENTER} key tap for first instance of {ENTER}. It
                # doesn't get recognized for some reason.
                if enter_idx is True and token in ("{ENTER}", "~"):
                    cmd = ['ydotool'] + YDOTOOL_AUTOTYPE_TOKENS[token]
                    call(cmd)
                    enter_idx = False
            else:
                dmenu_err("Unsupported auto-type token (ydotool): \"%s\"" % (token))
                return
        else:
            call(['ydotool', 'type', token])


def type_text(data):
    """Type the given text data

    """
    library = 'pynput'
    if CONF.has_option('database', 'type_library'):
        library = CONF.get('database', 'type_library')
    if library == 'xdotool':
        call(['xdotool', 'type', data])
    elif library == 'ydotool':
        call(['ydotool', 'type', data])
    else:
        kbd = keyboard.Controller()
        try:
            kbd.type(data)
        except kbd.InvalidCharacterException:
            dmenu_err("Unable to type string...bad character.\n"
                      "Try setting `type_library = xdotool` in config.ini")


def view_all_entries(options, entries_descriptions, prompt='Entries'):
    """Generate numbered list of all Keepass entries and open with dmenu.

    Returns: dmenu selection

    """

    kp_entries_b = str("\n").join(entries_descriptions).encode(ENC)
    if options:
        options_b = ("\n".join(map(str, options)) + "\n").encode(ENC)
        entries_b = options_b + kp_entries_b
    else:
        entries_b = kp_entries_b
    return dmenu_select(min(DMENU_LEN, len(options) + len(entries_descriptions)), prompt, inp=entries_b)


def select_group(kpo, prompt="Groups"):
    """Select which group for an entry

    Args: kpo - Keepass object
          options - list of menu options for groups

    Returns: False for no entry
             group - string

    """
    groups = kpo.groups
    num_align = len(str(len(groups)))
    pattern = str("{:>{na}} - {}")
    input_b = str("\n").join([pattern.format(j, i.path, na=num_align)
                              for j, i in enumerate(groups)]).encode(ENC)
    sel = dmenu_select(min(DMENU_LEN, len(groups)), prompt, inp=input_b)
    if not sel:
        return False
    try:
        return groups[int(sel.split('-', 1)[0])]
    except (ValueError, TypeError):
        return False


def manage_groups(kpo):
    """Rename, create, move or delete groups

    Args: kpo - Keepass object
    Returns: Group object or False

    """
    edit = True
    options = ['Create',
               'Move',
               'Rename',
               'Delete']
    group = False
    while edit is True:
        input_b = b"\n".join(i.encode(ENC) for i in options) + b"\n\n" + \
            b"\n".join(i.path.encode(ENC) for i in kpo.groups)
        sel = dmenu_select(len(options) + len(kpo.groups) + 1, "Groups", inp=input_b)
        if not sel:
            edit = False
        elif sel == 'Create':
            group = create_group(kpo)
        elif sel == 'Move':
            group = move_group(kpo)
        elif sel == 'Rename':
            group = rename_group(kpo)
        elif sel == 'Delete':
            group = delete_group(kpo)
        else:
            edit = False
    return group


def create_group(kpo):
    """Create new group

    Args: kpo - Keepass object
    Returns: Group object or False

    """
    parentgroup = select_group(kpo, prompt="Select parent group")
    if not parentgroup:
        return False
    name = dmenu_select(1, "Group name")
    if not name:
        return False
    group = kpo.add_group(parentgroup, name)
    kpo.save()
    return group


def delete_group(kpo):
    """Delete a group

    Args: kpo - Keepass object
    Returns: Group object or False

    """
    group = select_group(kpo, prompt="Delete Group:")
    if not group:
        return False
    input_b = b"NO\nYes - confirm delete\n"
    delete = dmenu_select(2, "Confirm delete", inp=input_b)
    if delete != "Yes - confirm delete":
        return True
    kpo.delete_group(group)
    kpo.save()
    return group


def move_group(kpo):
    """Move group

    Args: kpo - Keepass object
    Returns: Group object or False

    """
    group = select_group(kpo, prompt="Select group to move")
    if not group:
        return False
    destgroup = select_group(kpo, prompt="Select destination group")
    if not destgroup:
        return False
    group = kpo.move_group(group, destgroup)
    kpo.save()
    return group


def rename_group(kpo):
    """Rename group

    Args: kpo - Keepass object
    Returns: Group object or False

    """
    group = select_group(kpo, prompt="Select group to rename")
    if not group:
        return False
    name = dmenu_select(1, "New group name", inp=group.name.encode(ENC))
    if not name:
        return False
    group.name = name
    kpo.save()
    return group


def add_entry(kpo):
    """Add Keepass entry

    Args: kpo - Keepass object
    Returns: False if not added
             Keepass Entry object on success

    """
    group = select_group(kpo)
    if group is False:
        return False
    entry = kpo.add_entry(destination_group=group, title="", username="", password="")
    edit = True
    while edit is True:
        edit = edit_entry(kpo, entry)
    return entry


def delete_entry(kpo, kp_entry):
    """Delete an entry

    Args: kpo - Keepass object
          kp_entry - keepass entry
    Returns: True if no delete
             False if delete

    """
    input_b = b"NO\nYes - confirm delete\n"
    delete = dmenu_select(2, "Confirm delete", inp=input_b)
    if delete != "Yes - confirm delete":
        return True
    kpo.delete_entry(kp_entry)
    kpo.save()
    return False


def view_entry(kp_entry):
    """Show title, username, password, url and notes for an entry.

    Returns: dmenu selection

    """
    fields = [kp_entry.path or "Title: None",
              kp_entry.username or "Username: None",
              '**********' if kp_entry.password else "Password: None",
              kp_entry.url or "URL: None",
              "Notes: <Enter to view>" if kp_entry.notes else "Notes: None"]

    def show_prop(key):
        val = kp_entry.custom_properties[key]
        return '*********' if key.startswith('#') else val

    fields += [f'@({key}): {show_prop(key)}' for key in kp_entry.custom_properties]

    kp_entries_b = "\n".join(fields).encode(ENC)
    sel = dmenu_select(len(fields), inp=kp_entries_b)
    if sel == "Notes: <Enter to view>":
        sel = view_notes(kp_entry.notes)
    elif sel == "Notes: None":
        sel = ""
    elif sel == '**********':
        sel = kp_entry.password
    elif sel == fields[3]:
        if sel != "URL: None":
            webbrowser.open(sel)
        sel = ""
    elif re.search('^@', str(sel)):
        return kp_entry.custom_properties[re.search(r'@\((.+)(?=\):)', str(sel)).group(1)]
    return sel


def edit_entry(kpo, kp_entry):  # pylint: disable=too-many-return-statements, too-many-branches
    """Edit title, username, password, url and autotype sequence for an entry.

    Args: kpo - Keepass object
          kp_entry - selected Entry object

    Returns: True to continue editing
             False if done

    """
    fields = [str("Title: {}").format(kp_entry.title),
              str("Path: {}").format(kp_entry.path.rstrip(kp_entry.title)),
              str("Username: {}").format(kp_entry.username),
              str("Password: **********") if kp_entry.password else "Password: None",
              str("Url: {}").format(kp_entry.url),
              "Notes: <Enter to Edit>" if kp_entry.notes else "Notes: None",
              "Delete Entry: "]
    if hasattr(kp_entry, 'autotype_sequence') and hasattr(kp_entry, 'autotype_enabled'):
        fields[5:5] = [str("Autotype Sequence: {}").format(kp_entry.autotype_sequence),
                       str("Autotype Enabled: {}").format(kp_entry.autotype_enabled)]
    input_b = "\n".join(fields).encode(ENC)
    sel = dmenu_select(len(fields), inp=input_b)
    try:
        field, sel = sel.split(": ", 1)
    except (ValueError, TypeError):
        return False
    field = field.lower().replace(" ", "_")
    if field == 'password':
        sel = kp_entry.password
    edit_b = sel.encode(ENC) + b"\n" if sel is not None else b"\n"
    if field == 'delete_entry':
        return delete_entry(kpo, kp_entry)
    if field == 'path':
        group = select_group(kpo)
        if not group:
            return True
        kpo.move_entry(kp_entry, group)
        return True
    pw_choice = ""
    if field == 'password':
        inputs_b = [
            b"Generate password",
            b"Manually enter password",
        ]
        if kp_entry.password:
            inputs_b.append(b"Type existing password")
        pw_choice = dmenu_select(len(inputs_b), "Password", inp=b"\n".join(inputs_b))
        if pw_choice == "Manually enter password":
            pass
        elif pw_choice == "Type existing password":
            type_text(kp_entry.password)
            return False
        elif not pw_choice:
            return True
        else:
            pw_choice = ''
            input_b = b"20\n"
            length = dmenu_select(1, "Password Length?", inp=input_b)
            if not length:
                return True
            try:
                length = int(length)
            except ValueError:
                length = 20
            chars = get_password_chars()
            if chars is False:
                return True
            sel = gen_passwd(chars, length)
            if sel is False:
                dmenu_err("Number of char groups desired is more than requested pw length")
                return True

    if field == 'autotype_enabled':
        input_b = b"True\nFalse\n"
        at_enab = dmenu_select(2, "Autotype Enabled? True/False", inp=input_b)
        if not at_enab:
            return True
        sel = not at_enab == 'False'
    if (field not in ('password', 'notes', 'path', 'autotype_enabled')) or pw_choice:
        sel = dmenu_select(1, "{}".format(field.capitalize()), inp=edit_b)
        if not sel:
            return True
        if pw_choice:
            sel_check = dmenu_select(1, "{}".format(field.capitalize()), inp=edit_b)
            if not sel_check or sel_check != sel:
                dmenu_err("Passwords do not match. No changes made.")
                return True
    elif field == 'notes':
        sel = edit_notes(kp_entry.notes)
    setattr(kp_entry, field, sel)
    return True


def edit_notes(note):
    """Use $EDITOR (or 'vim' if not set) to edit the notes entry

    In configuration file:
        Set 'gui_editor' for things like emacs, gvim, leafpad
        Set 'editor' for vim, emacs -nw, nano unless $EDITOR is defined
        Set 'terminal' if using a non-gui editor

    Args: note - string
    Returns: note - string

    """
    if CONF.has_option("database", "gui_editor"):
        editor = CONF.get("database", "gui_editor")
        editor = shlex.split(editor)
    else:
        if CONF.has_option("database", "editor"):
            editor = CONF.get("database", "editor")
        else:
            editor = os.environ.get('EDITOR', 'vim')
        if CONF.has_option("database", "terminal"):
            terminal = CONF.get("database", "terminal")
        else:
            terminal = "xterm"
        terminal = shlex.split(terminal)
        editor = shlex.split(editor)
        editor = terminal + ["-e"] + editor
    note = b'' if note is None else note.encode(ENC)
    with tempfile.NamedTemporaryFile(suffix=".tmp") as fname:
        fname.write(note)
        fname.flush()
        editor.append(fname.name)
        try:
            call(editor)
        except FileNotFoundError:
            dmenu_err("Terminal not found. Please update config.ini.")
            note = '' if not note else note.decode(ENC)
            return note
        fname.seek(0)
        note = fname.read()
    note = '' if not note else note.decode(ENC)
    return note


def view_notes(notes):
    """View the 'Notes' field line-by-line within dmenu.

    Returns: text of the selected line for typing

    """
    notes_l = notes.split('\n')
    notes_b = "\n".join(notes_l).encode(ENC)
    sel = dmenu_select(min(DMENU_LEN, len(notes_l)), inp=notes_b)
    return sel


# extract _entry_description and _description_idx functions here to be able to
# easily change format of displayed entry

def _entry_description(idx, idx_align, e):
    "return text describing entry (used to select entry from menu)"
    return f'{idx:>{idx_align}} - {e.path} - {e.username} - {e.url}'

def _description_idx(description):
    "extract entry idx from text used to select it"
    return int(description.split('-', 1)[0])


class DmenuRunner(Process):
    """Listen for dmenu calling event and run keepmenu

    Args: server - Server object
          kpo - Keepass object
    """
    def __init__(self, server):
        Process.__init__(self)
        self.server = server
        self.database = get_database()
        self.kpo = get_entries(self.database)
        if not self.kpo:
            self.server.kill_flag.set()
            sys.exit()

        self.actions = {
            MenuOption.TypePassword:self.type_password,
            MenuOption.TypeUsername:self.type_username,
            MenuOption.TypeEntry:self.type_entry,
            MenuOption.ViewEntry:self.view_entry,
            MenuOption.Edit:self.edit_entry,
            MenuOption.Add:self.add_entry,
            MenuOption.ManageGroups:self.manage_groups,
            MenuOption.ReloadDB:self.reload_db,
            MenuOption.KillDaemon:self.kill_daemon
        }

    def _set_timer(self):
        """Set inactivity timer

        """
        self.cache_timer = Timer(CACHE_PERIOD_MIN * 60, self.cache_time)
        self.cache_timer.daemon = True
        self.cache_timer.start()

    def run(self):
        while True:
            option = self.server.start_q.get()
            if self.server.kill_flag.is_set():
                break
            if not self.kpo:
                pass
            else:
                self.dmenu_run(option)
            if self.server.cache_time_expired.is_set():
                self.server.kill_flag.set()
            if self.server.kill_flag.is_set():
                break

    def cache_time(self):
        """Kill keepmenu daemon when cache timer expires

        """
        self.server.cache_time_expired.set()
        if self.server.start_q.empty():
            self.server.kill_flag.set()
            self.server.start_q.set()

    def dmenu_run(self, option):
        """Run dmenu with the given list of Keepass Entry objects

        If 'hide_groups' is defined in config.ini, hide those from main and
        view/type all views.

        Args: self.kpo - Keepass object

        Note: I had to reload the kpo object after every save to prevent being
        affected by the gibberish password bug in pykeepass:
        https://github.com/pschmitt/pykeepass/issues/43

        Once this is fixed, the extra calls to self.kpo = get_entries... can be
        deleted

        """
        try:
            self.cache_timer.cancel()
        except AttributeError:
            pass

        self._set_timer()

        if option is None:
            option = self.dmenu_select_option()

        if option:
            action = self.actions[option]
            finished = action(prompt=option.description())

            if not finished:
                self.dmenu_run(None)

    def dmenu_select_option(self):
        selection = view_all_entries(
            [],
            [f'{op.description()}' for op in self.actions.keys()],
            prompt='Select action'
        )

        return next(
            (x for x in self.actions if x.description() == selection),
            None
        )

    def type_entry(self, prompt=None):
        sel = self.dmenu_select(prompt)

        if sel:
            entry = self.get_selected_entry(sel)
            type_entry(entry)
            return True

    def type_password(self, prompt=None):
        sel = self.dmenu_select(prompt)

        if sel:
            entry = self.get_selected_entry(sel)
            type_text(entry.password or '')
            return True

    def type_username(self, prompt=None):
        sel = self.dmenu_select(prompt)

        if sel:
            entry = self.get_selected_entry(sel)
            type_text(entry.username or '')
            return True

    def view_entry(self, prompt=None):
        sel = self.dmenu_select(prompt)

        if sel:
            entry = self.get_selected_entry(sel)
            text = view_entry(entry)
            type_text(text or '')
            return True

    def edit_entry(self, prompt=None):
        sel = self.dmenu_select(prompt, include_hidden=True)

        if sel:
            entry = self.get_selected_entry(sel)
            edit = True

            while edit is True:
                edit = edit_entry(self.kpo, entry)

            self.kpo.save()
            self.kpo = get_entries(self.database)
            return True

    def add_entry(self, **kwds):
        entry = add_entry(self.kpo)

        if entry:
            self.kpo.save()
            self.kpo = get_entries(self.database)
            return True

    def manage_groups(self, **kwds):
        group = manage_groups(self.kpo)

        if group:
            self.kpo.save()
            self.kpo = get_entries(self.database)
            return True

    def reload_db(self, **kwds):
        self.kpo = get_entries(self.database)

    def kill_daemon(self, **kwds):
        try:
            self.server.kill_flag.set()
        except (EOFError, IOError):
            pass

        return True

    def dmenu_select(self, prompt, *, include_hidden=False, options=None):
        kwds = {'prompt': prompt} if prompt else {}

        return view_all_entries(
            options or [],
            self.get_entries_descriptions(include_hidden=include_hidden),
            **kwds
        )

    def get_entries_descriptions(self, *, include_hidden=False):
        idx_align = len(str(len(self.kpo.entries)))

        return [
            _entry_description(idx, idx_align, entry)
            for idx, entry in enumerate(self.kpo.entries)
            if include_hidden or not self.is_hidden(entry)
        ]

    def get_selected_entry(self, description):
        if description:
            return self.kpo.entries[_description_idx(description)]

        return None

    def is_hidden(self, entry):
        entry_group = entry.path.rstrip(entry.title)
        return any(group in entry_group for group in self.get_hidden_groups())

    def get_hidden_groups(self):
        # Validate ignored group names in config.ini

        if CONF.has_option("database", "hide_groups"):
            return [
                hg for hg in CONF.get("database", "hide_groups").split(",")
                if hg in [
                    g.name for g in self.kpo.groups
                ]
            ]

        return []


class Server(Process):
    """Run BaseManager server to listen for dmenu calling events

    """
    def __init__(self):
        Process.__init__(self)
        self.port, self.authkey = get_auth()
        self.start_q = Queue()
        self.kill_flag = Event()
        self.cache_time_expired = Event()

    def run(self):
        serv = self.server()  # pylint: disable=unused-variable
        self.kill_flag.wait()

    def server(self):
        """Set up BaseManager server

        """
        mgr = BaseManager(address=('127.0.0.1', self.port),
                          authkey=self.authkey)
        mgr.register('show_dmenu', callable=self.show_dmenu)
        mgr.start()
        return mgr


    def show_dmenu(self, args):
        if args.type_password == True:
            default_option = MenuOption.TypePassword
        elif args.view_entry == True:
            default_option = MenuOption.ViewEntry
        elif args.type_username == True:
            default_option = MenuOption.TypeUsername
        elif args.type_entry == True:
            default_option = MenuOption.TypeEntry
        else:
            default_option = None

        self.start_q.put(default_option)


def client():
    """Define client connection to server BaseManager

    Returns: BaseManager object
    """
    port, auth = get_auth()
    mgr = BaseManager(address=('', port), authkey=auth)
    mgr.register('show_dmenu')
    mgr.connect()
    return mgr


def start_server(args):
    """Main entrypoint. Start the background Manager and Dmenu runner processes.

    """
    server = Server()
    dmenu = DmenuRunner(server)
    dmenu.daemon = True
    server.start()
    dmenu.start()
    server.show_dmenu(args)

    server.join()
    if exists(expanduser(AUTH_FILE)):
        os.remove(expanduser(AUTH_FILE))


def main():
    parser = argparse.ArgumentParser('keepmenu')
    parser.add_argument('--type-password', action='store_true', default='False', dest='type_password')
    parser.add_argument('--view-entry', action='store_true', default='False', dest='view_entry')
    parser.add_argument('--type-username', action='store_true', default='False', dest='type_username')
    parser.add_argument('--type-entry', action='store_true', default='False', dest='type_entry')
    args = parser.parse_args()

    try:
        MANAGER = client()
        MANAGER.show_dmenu(args)  # pylint: disable=no-member
    except socket.error:
        process_config()
        start_server(args)

if __name__ == '__main__':
    main()

# vim: set et ts=4 sw=4 :
