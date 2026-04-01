#!/usr/bin/env python3
"""
claude_status.py — Claude usage limits in the GNOME panel (AppIndicator3).

Config: ~/.config/claude-status/config.json
Debug:  python3 claude_status.py --debug
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Gdk', '3.0')

try:
    import cairo as _cairo
    HAS_CAIRO = True
except ImportError:
    HAS_CAIRO = False

from gi.repository import Gtk, AppIndicator3, GLib, GdkPixbuf, Gdk
import json
import os
import subprocess
import sys
import tempfile
import threading
import argparse
import atexit
from datetime import datetime
from pathlib import Path

CONFIG_DIR  = Path.home() / '.config' / 'claude-status'
CONFIG_FILE = CONFIG_DIR / 'config.json'

DEFAULT_CONFIG = {
    "display_mode":       "both",       # "percent" | "bar" | "both"
    "show_value":         "remaining",  # "remaining" | "used"
    "bar_color":          "#00B4D8",    # hex colour (used when danger_bar is off)
    "track_color":        "#464646",    # bar track / background colour
    "bar_width":          52,           # px
    "bar_height":         8,            # px
    "bar_size":           "medium",    # "small" | "medium" | "large" | "xl"
    "show_type":          "5hr",        # "5hr" | "weekly"
    "refresh_interval":   120,          # seconds
    "session_key":        "",
    "danger_bar":         False,        # colour bar green→yellow→orange→red
    "danger_text":        False,        # colour text the same way
    "danger_yellow_pct":  50,           # used% at which bar turns yellow
    "danger_orange_pct":  75,           # used% at which bar turns orange
    "danger_red_pct":     90,           # used% at which bar turns red
    "label_side":         "right",      # "left" | "right"  (in 'both' mode)
    "panel_order":        "right",      # "left" | "right" ordering within tray cluster
    "show_logo":          False,        # show a 'C' glyph next to the bar
    "notify_at_pct":      0,            # 0 = off; send desktop notification when used% >= this
}

ICON_H = 22   # standard tray icon height (px)
BAR_SIZE_PRESETS = {
    'small': (44, 7),
    'medium': (52, 8),
    'large': (72, 11),
    'xl': (92, 13),
}


# ── Colour helpers ─────────────────────────────────────────────────────────────

def _hex_to_rgb(h):
    h = str(h).strip().lstrip('#')
    if len(h) != 6:
        return 255, 255, 255
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 255, 255, 255


def _is_valid_hex_color(value):
    value = str(value).strip()
    if len(value) != 7 or not value.startswith('#'):
        return False
    try:
        int(value[1:], 16)
        return True
    except ValueError:
        return False


def _danger_color(used_pct, cfg):
    if used_pct >= cfg['danger_red_pct']:
        return '#F44336'
    if used_pct >= cfg['danger_orange_pct']:
        return '#FF9800'
    if used_pct >= cfg['danger_yellow_pct']:
        return '#FFC107'
    return '#4CAF50'


# ── Icon rendering ─────────────────────────────────────────────────────────────

def _surface_to_pixbuf(surface):
    """Convert Cairo ARGB32 premultiplied surface → GdkPixbuf RGBA."""
    w, h   = surface.get_width(), surface.get_height()
    stride = surface.get_stride()
    raw    = surface.get_data()
    rgba   = bytearray(w * h * 4)
    for y in range(h):
        rs = y * stride
        for x in range(w):
            ci = rs + x * 4
            # Cairo ARGB32 little-endian byte order: B G R A
            bv, gv, rv, av = raw[ci], raw[ci+1], raw[ci+2], raw[ci+3]
            if av > 0:   # un-premultiply
                rv = min(255, rv * 255 // av)
                gv = min(255, gv * 255 // av)
                bv = min(255, bv * 255 // av)
            ri = (y * w + x) * 4
            rgba[ri:ri+4] = [rv, gv, bv, av]
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(rgba)),
        GdkPixbuf.Colorspace.RGB, True, 8, w, h, w * 4,
    )


def _draw_logo(ctx, x, color_hex):
    """Draw a visible logo mark that survives panel scaling."""
    r, g, b = _hex_to_rgb(color_hex)
    cx = x + 9
    cy = ICON_H / 2

    # Ring for contrast on any panel background.
    ctx.set_source_rgba(r/255, g/255, b/255, 0.95)
    ctx.set_line_width(2.0)
    ctx.arc(cx, cy, 6.2, 0, 6.283185307179586)
    ctx.stroke()

    # Inner glyph.
    ctx.set_source_rgba(r/255, g/255, b/255, 1.0)
    ctx.select_font_face('Sans', _cairo.FONT_SLANT_NORMAL, _cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(11)
    ctx.move_to(x + 5, 15)
    ctx.show_text('C')


def _draw_label_text(ctx, text, color_hex, x, width):
    """Draw text horizontally centred in a *width*-px region."""
    r, g, b = _hex_to_rgb(color_hex)
    ctx.set_source_rgba(r/255, g/255, b/255, 1.0)
    ctx.select_font_face('Sans', _cairo.FONT_SLANT_NORMAL, _cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(11)
    ext = ctx.text_extents(text)
    tx  = x + max(0, (width - ext.width) / 2)
    ctx.move_to(tx, 15)
    ctx.show_text(text)


def _draw_bar(ctx, fill_pct, color_hex, track_hex, x_off, bar_w, bar_h):
    top  = (ICON_H - bar_h) // 2
    fill = int(bar_w * max(0.0, min(100.0, fill_pct)) / 100.0)
    # Track (dark background)
    tr, tg, tb = _hex_to_rgb(track_hex)
    ctx.set_source_rgba(tr/255, tg/255, tb/255, 0.75)
    ctx.rectangle(x_off, top, bar_w, bar_h)
    ctx.fill()
    # Filled portion
    if fill > 0:
        r, g, b = _hex_to_rgb(color_hex)
        ctx.set_source_rgba(r/255, g/255, b/255, 1.0)
        ctx.rectangle(x_off, top, fill, bar_h)
        ctx.fill()


def _make_icon_pixbuf(fill_pct, bar_color, text_str, text_color, cfg):
    """Render the tray icon as a GdkPixbuf using Cairo.

    fill_pct   : 0–100 how much of the bar to fill
    bar_color  : hex colour for bar fill
    text_str   : text to embed in icon, or None
    text_color : hex colour for embedded text
    cfg        : full config dict
    """
    if not HAS_CAIRO:
        return _make_bar_pixbuf_simple(fill_pct, bar_color, cfg)

    mode       = cfg['display_mode']
    label_side = cfg.get('label_side', 'right')
    show_logo  = cfg.get('show_logo', False)
    show_bar   = mode in ('bar', 'both')

    LOGO_W = 18 if show_logo else 0
    BAR_W  = cfg['bar_width'] if show_bar else 0
    TEXT_W = (max(len(text_str) * 7 + 4, 24) if text_str else 0)

    # Layout order depends on side
    if label_side == 'left' and text_str:
        total_w = LOGO_W + TEXT_W + BAR_W
    else:
        total_w = LOGO_W + BAR_W + TEXT_W

    total_w = max(total_w, 8)

    surface = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, total_w, ICON_H)
    ctx = _cairo.Context(surface)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    x = 0

    if show_logo:
        _draw_logo(ctx, x, bar_color)
        x += LOGO_W

    if label_side == 'left' and text_str:
        _draw_label_text(ctx, text_str, text_color, x, TEXT_W)
        x += TEXT_W

    if show_bar:
        _draw_bar(ctx, fill_pct, bar_color, cfg['track_color'], x,
                  cfg['bar_width'], cfg['bar_height'])
        x += BAR_W

    if label_side == 'right' and text_str:
        _draw_label_text(ctx, text_str, text_color, x + 2, TEXT_W)

    return _surface_to_pixbuf(surface)


def _make_bar_pixbuf_simple(fill_pct, color_hex, cfg):
    """Fallback when Cairo is unavailable — plain bar, no text or logo."""
    r, g, b  = _hex_to_rgb(color_hex)
    tr, tg, tb = _hex_to_rgb(cfg.get('track_color', '#464646'))
    bar_w    = cfg['bar_width']
    bar_h    = cfg['bar_height']
    top      = (ICON_H - bar_h) // 2
    fill     = int(bar_w * max(0.0, min(100.0, fill_pct)) / 100.0)
    data     = bytearray(bar_w * ICON_H * 4)
    for y in range(ICON_H):
        for x in range(bar_w):
            i = (y * bar_w + x) * 4
            if top <= y < top + bar_h:
                data[i:i+4] = [r, g, b, 255] if x < fill else [tr, tg, tb, 190]
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(data)),
        GdkPixbuf.Colorspace.RGB, True, 8, bar_w, ICON_H, bar_w * 4,
    )


# ── Main app ───────────────────────────────────────────────────────────────────

class ClaudeStatus:
    def __init__(self, debug=False):
        self.debug  = debug
        self.config = self._load_config()
        self._usage = None
        self._error = None
        self._notified_threshold = None   # tracks which threshold we've already fired

        # Two temp files; alternate to force AppIndicator3 to reload the icon.
        # Use mkstemp instead of mktemp to avoid TOCTOU races.
        self._icon_paths = [self._new_temp_png_path(), self._new_temp_png_path()]
        self._icon_idx   = 0
        atexit.register(self._cleanup_temp_icons)

        ph = _make_icon_pixbuf(0.0, self.config['bar_color'], None, None, self.config)
        for p in self._icon_paths:
            ph.savev(p, 'png', [], [])

        self.indicator = AppIndicator3.Indicator.new(
            'claude-status', self._icon_paths[0],
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label('…', '100%')
        self._apply_panel_order()

        self._build_menu()
        self.indicator.set_menu(self.menu)

        self._schedule_fetch()
        GLib.timeout_add_seconds(self.config['refresh_interval'], self._schedule_fetch)

    # ── Config ─────────────────────────────────────────────────────────────────

    def _load_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    loaded = {**DEFAULT_CONFIG, **json.load(f)}
            except Exception as e:
                if self.debug:
                    print(f'[config] invalid config, resetting to defaults: {e}')
                loaded = DEFAULT_CONFIG.copy()
            normalized = self._normalize_config(loaded)
            if normalized != loaded:
                with open(CONFIG_FILE, 'w') as wf:
                    json.dump(normalized, wf, indent=2)
            return normalized
        with open(CONFIG_FILE, 'w') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return DEFAULT_CONFIG.copy()

    def _normalize_config(self, cfg):
        out = {**DEFAULT_CONFIG, **cfg}

        def _as_int(value, default):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _as_bool(value, default=False):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ('1', 'true', 'yes', 'on'):
                    return True
                if low in ('0', 'false', 'no', 'off'):
                    return False
            return default

        if out.get('display_mode') not in ('percent', 'bar', 'both'):
            out['display_mode'] = DEFAULT_CONFIG['display_mode']
        if out.get('show_value') not in ('remaining', 'used'):
            out['show_value'] = DEFAULT_CONFIG['show_value']
        if out.get('show_type') not in ('5hr', 'weekly'):
            out['show_type'] = DEFAULT_CONFIG['show_type']
        if out.get('label_side') not in ('left', 'right'):
            out['label_side'] = DEFAULT_CONFIG['label_side']
        if out.get('panel_order') not in ('left', 'right'):
            out['panel_order'] = DEFAULT_CONFIG['panel_order']
        if out.get('bar_size') not in BAR_SIZE_PRESETS:
            out['bar_size'] = DEFAULT_CONFIG['bar_size']

        for ck in ('bar_color', 'track_color'):
            if not _is_valid_hex_color(out.get(ck)):
                out[ck] = DEFAULT_CONFIG[ck]

        out['bar_width'] = _as_int(out.get('bar_width', DEFAULT_CONFIG['bar_width']), -1)
        out['bar_height'] = _as_int(out.get('bar_height', DEFAULT_CONFIG['bar_height']), -1)
        if out['bar_width'] <= 0 or out['bar_height'] <= 0:
            out['bar_width'], out['bar_height'] = BAR_SIZE_PRESETS[out['bar_size']]

        out['bar_width'] = max(26, min(220, out['bar_width']))
        out['bar_height'] = max(4, min(18, out['bar_height']))

        out['refresh_interval'] = _as_int(out.get('refresh_interval', 120), 120)
        out['refresh_interval'] = max(15, out['refresh_interval'])

        out['notify_at_pct'] = _as_int(out.get('notify_at_pct', 0), 0)
        out['notify_at_pct'] = max(0, min(100, out['notify_at_pct']))

        out['danger_yellow_pct'] = max(1, min(100, _as_int(out.get('danger_yellow_pct', 50), 50)))
        out['danger_orange_pct'] = max(1, min(100, _as_int(out.get('danger_orange_pct', 75), 75)))
        out['danger_red_pct'] = max(1, min(100, _as_int(out.get('danger_red_pct', 90), 90)))
        out['danger_yellow_pct'], out['danger_orange_pct'], out['danger_red_pct'] = sorted(
            [out['danger_yellow_pct'], out['danger_orange_pct'], out['danger_red_pct']]
        )

        out['danger_bar'] = _as_bool(out.get('danger_bar', False), False)
        out['danger_text'] = _as_bool(out.get('danger_text', False), False)
        out['show_logo'] = _as_bool(out.get('show_logo', False), False)
        return out

    def _save_config(self):
        self.config = self._normalize_config(self.config)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=2)

    def _apply_panel_order(self):
        # AppIndicator can reorder within the tray side; GNOME decides panel side.
        idx = 0 if self.config.get('panel_order', 'right') == 'left' else 10000
        try:
            self.indicator.set_ordering_index(idx)
        except Exception:
            if self.debug:
                print('[ui] set_ordering_index not supported by this shell/extension')

    # ── Icon helpers ───────────────────────────────────────────────────────────

    def _next_icon_path(self):
        self._icon_idx ^= 1
        return self._icon_paths[self._icon_idx]

    @staticmethod
    def _new_temp_png_path():
        fd, path = tempfile.mkstemp(suffix='.png', prefix='claude-status-')
        os.close(fd)
        return path

    def _cleanup_temp_icons(self):
        for p in self._icon_paths:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
            except Exception as e:
                if self.debug:
                    print(f'[cleanup] failed to delete {p}: {e}')

    # ── Network ────────────────────────────────────────────────────────────────

    def _get_oauth_token(self):
        creds_file = Path.home() / '.claude' / '.credentials.json'
        try:
            with open(creds_file) as f:
                data = json.load(f)
            token = data.get('claudeAiOauth', {}).get('accessToken', '').strip()
            return token or None
        except Exception as e:
            if self.debug:
                print(f'[auth] could not read credentials: {e}')
            return None

    def _fetch(self):
        token = self._get_oauth_token()
        if not token:
            return None, (
                'No OAuth token found.\n'
                'Run `claude` and log in, then restart this indicator.'
            )

        node_script = r"""
const https = require('https');
const fs = require('fs');
const token = fs.readFileSync(0, 'utf8').trim();
const options = {
  hostname: 'claude.ai',
  path: '/api/oauth/usage',
  method: 'GET',
  headers: {
        'x-api-key': token,
    'Content-Type': 'application/json',
    'User-Agent': 'claude-code/2.1.89',
    'anthropic-client-version': '2.1.89',
    'anthropic-client-platform': 'cli-linux',
  }
};
const req = https.request(options, (res) => {
  let data = '';
  res.on('data', (chunk) => data += chunk);
  res.on('end', () => {
    process.stdout.write(JSON.stringify({status: res.statusCode, body: data}));
  });
});
req.on('error', (e) => {
  process.stdout.write(JSON.stringify({error: e.message}));
});
req.end();
"""
        import shutil
        import glob as _glob

        node_bin = shutil.which('node')
        if not node_bin:
            candidates = _glob.glob(
                str(Path.home() / '.nvm' / 'versions' / 'node' / '*' / 'bin' / 'node')
            )
            if candidates:
                node_bin = sorted(candidates)[-1]
        if not node_bin:
            return None, 'node not found. Install Node.js to use this indicator.'

        try:
            result = subprocess.run(
                [node_bin, '-e', node_script],
                capture_output=True,
                text=True,
                timeout=15,
                input=token,
            )
            if self.debug:
                print(f'[node] stdout: {result.stdout[:800]}')
                print(f'[node] stderr: {result.stderr[:200]}')
            if result.returncode != 0:
                return None, f'node error: {result.stderr[:200]}'
            resp = json.loads(result.stdout)
            if 'error' in resp:
                return None, f'network error: {resp["error"]}'
            if resp['status'] != 200:
                return None, f'API error {resp["status"]}: {resp["body"][:200]}'
            data = json.loads(resp['body'])
            if self.debug:
                print(f'[usage] {json.dumps(data, indent=2)}')
            return self._parse_usage(data), None
        except subprocess.TimeoutExpired:
            return None, 'Request timed out.'
        except Exception as e:
            return None, str(e)

    def _parse_usage(self, data):
        show   = self.config.get('show_type', '5hr')
        bucket = data.get('seven_day') if show == 'weekly' else data.get('five_hour')
        if not bucket or bucket.get('utilization') is None:
            bucket = data.get('five_hour') or data.get('seven_day')
        if not bucket or bucket.get('utilization') is None:
            return None
        used_pct  = float(bucket['utilization'])
        remaining = round(100.0 - used_pct, 1)
        return {
            'remaining':  remaining,
            'used':       round(used_pct, 1),
            'limit':      100,
            'percent':    remaining,      # kept for compatibility
            'resets_at':  bucket.get('resets_at'),
            '_five_hour': data.get('five_hour'),
            '_seven_day': data.get('seven_day'),
        }

    # ── Refresh loop ───────────────────────────────────────────────────────────

    def _schedule_fetch(self):
        threading.Thread(target=self._bg_fetch, daemon=True).start()
        return True

    def _bg_fetch(self):
        usage, error = self._fetch()
        GLib.idle_add(self._apply_update, usage, error)

    def _apply_update(self, usage, error):
        self._usage = usage
        self._error = error
        mode = self.config['display_mode']

        if error or not usage:
            self.indicator.set_label('?', '?')
            if hasattr(self, '_status_item'):
                self._status_item.set_label((error or 'No data').split('\n')[0][:80])
            if self.debug and error:
                print(f'[error] {error}')
            return

        used_pct      = usage['used']
        remaining_pct = usage['remaining']

        show_value   = self.config.get('show_value', 'remaining')
        display_pct  = used_pct if show_value == 'used' else remaining_pct
        display_text = f'{display_pct:.0f}%'
        fill_pct     = display_pct

        # Resolve bar colour
        bar_color = self.config['bar_color']
        if self.config.get('danger_bar'):
            bar_color = _danger_color(used_pct, self.config)

        # Resolve text colour
        text_color = self.config['bar_color']
        if self.config.get('danger_text'):
            text_color = _danger_color(used_pct, self.config)

        # Whether to embed the text inside the icon pixbuf.
        # Required when: label should be left-of-bar, OR text needs a danger colour
        # (AppIndicator set_label() doesn't support colour).
        label_side   = self.config.get('label_side', 'right')
        danger_text  = self.config.get('danger_text', False)
        showing_text = mode in ('percent', 'both')

        embed_in_icon = showing_text and (
            danger_text or
            mode == 'percent' or
            (mode == 'both' and label_side == 'left')
        )

        icon_text  = display_text if embed_in_icon else None
        icon_t_clr = text_color   if embed_in_icon else None

        # Build icon
        pb   = _make_icon_pixbuf(fill_pct, bar_color,
                                  icon_text, icon_t_clr or bar_color, self.config)
        path = self._next_icon_path()
        pb.savev(path, 'png', [], [])
        self.indicator.set_icon_full(path, f'Claude: {display_text}')

        # Panel label text (right of icon)
        if showing_text and not embed_in_icon:
            self.indicator.set_label(display_text, '100%')
        else:
            self.indicator.set_label('', '')

        # Status menu item
        if hasattr(self, '_status_item'):
            reset_str  = self._format_reset(usage.get('resets_at'))
            label_word = 'remaining' if show_value == 'remaining' else 'used'
            self._status_item.set_label(f'{display_text} {label_word}{reset_str}')

        self._check_notification(used_pct)

    @staticmethod
    def _format_reset(resets_at):
        if not resets_at:
            return ''
        try:
            if isinstance(resets_at, (int, float)):
                dt = datetime.fromtimestamp(resets_at).astimezone()
            else:
                dt = datetime.fromisoformat(
                    str(resets_at).replace('Z', '+00:00')
                ).astimezone()
            diff = dt - datetime.now().astimezone()
            secs = diff.total_seconds()
            if secs <= 0:
                return ' (resetting…)'
            h, rem = divmod(int(secs), 3600)
            m = rem // 60
            return f' — resets in {h}h {m:02d}m'
        except Exception:
            return ''

    # ── Notifications ──────────────────────────────────────────────────────────

    def _check_notification(self, used_pct):
        notify_at = self.config.get('notify_at_pct', 0)
        if notify_at <= 0:
            self._notified_threshold = None
            return
        if used_pct >= notify_at and self._notified_threshold != notify_at:
            self._notified_threshold = notify_at
            self._send_notification(used_pct, notify_at)
        elif used_pct < notify_at:
            self._notified_threshold = None   # reset so it fires again if usage spikes later

    def _send_notification(self, used_pct, threshold):
        remaining = 100 - used_pct
        msg = f'{used_pct:.0f}% used — {remaining:.0f}% remaining'
        try:
            subprocess.run(
                ['notify-send', '--urgency=normal', '--icon=dialog-warning',
                 'Claude Usage Alert', msg],
                capture_output=True, timeout=5,
            )
        except Exception as e:
            if self.debug:
                print(f'[notify] failed: {e}')

    # ── Menu ───────────────────────────────────────────────────────────────────

    def _build_menu(self):
        self.menu = Gtk.Menu()

        # ── Status line (non-interactive) ─────────────────────────────────────
        self._status_item = Gtk.MenuItem(label='Loading…')
        self._status_item.set_sensitive(False)
        self.menu.append(self._status_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        # ── Display mode ──────────────────────────────────────────────────────
        disp_item = Gtk.MenuItem(label='Display mode')
        disp_menu = Gtk.Menu()
        disp_item.set_submenu(disp_menu)
        group = []
        for val, lbl in [('both', 'Bar + Percentage'), ('bar', 'Bar only'),
                         ('percent', 'Percentage only')]:
            ri = Gtk.RadioMenuItem.new_with_label(group, lbl)
            group = ri.get_group()
            ri.set_active(self.config['display_mode'] == val)
            ri.connect('toggled', self._on_display_mode, val)
            disp_menu.append(ri)
        self.menu.append(disp_item)

        # ── Show value ────────────────────────────────────────────────────────
        val_item = Gtk.MenuItem(label='Show value')
        val_menu = Gtk.Menu()
        val_item.set_submenu(val_menu)
        group = []
        for val, lbl in [('remaining', 'Remaining %'), ('used', 'Used %')]:
            ri = Gtk.RadioMenuItem.new_with_label(group, lbl)
            group = ri.get_group()
            ri.set_active(self.config.get('show_value', 'remaining') == val)
            ri.connect('toggled', self._on_show_value, val)
            val_menu.append(ri)
        self.menu.append(val_item)

        # ── Limit shown ───────────────────────────────────────────────────────
        limit_item = Gtk.MenuItem(label='Limit shown')
        limit_menu = Gtk.Menu()
        limit_item.set_submenu(limit_menu)
        group = []
        for val, lbl in [('5hr', '5-hour window'), ('weekly', 'Weekly')]:
            ri = Gtk.RadioMenuItem.new_with_label(group, lbl)
            group = ri.get_group()
            ri.set_active(self.config.get('show_type', '5hr') == val)
            ri.connect('toggled', self._on_show_type, val)
            limit_menu.append(ri)
        self.menu.append(limit_item)

        # ── Label side ────────────────────────────────────────────────────────
        side_item = Gtk.MenuItem(label='Label side')
        side_menu = Gtk.Menu()
        side_item.set_submenu(side_menu)
        group = []
        for val, lbl in [('right', 'Right of bar'), ('left', 'Left of bar')]:
            ri = Gtk.RadioMenuItem.new_with_label(group, lbl)
            group = ri.get_group()
            ri.set_active(self.config.get('label_side', 'right') == val)
            ri.connect('toggled', self._on_label_side, val)
            side_menu.append(ri)
        self.menu.append(side_item)

        # ── Tray order ───────────────────────────────────────────────────────
        tray_item = Gtk.MenuItem(label='Tray order')
        tray_menu = Gtk.Menu()
        tray_item.set_submenu(tray_menu)
        group = []
        for val, lbl in [('left', 'Pin further left in tray'),
                         ('right', 'Pin further right in tray')]:
            ri = Gtk.RadioMenuItem.new_with_label(group, lbl)
            group = ri.get_group()
            ri.set_active(self.config.get('panel_order', 'right') == val)
            ri.connect('toggled', self._on_panel_order, val)
            tray_menu.append(ri)
        tray_menu.append(Gtk.SeparatorMenuItem())
        tray_note = Gtk.MenuItem(label='Note: GNOME controls true left/right panel side')
        tray_note.set_sensitive(False)
        tray_menu.append(tray_note)
        self.menu.append(tray_item)

        # ── Show logo ─────────────────────────────────────────────────────────
        logo_item = Gtk.CheckMenuItem(label='Show Claude logo')
        logo_item.set_active(self.config.get('show_logo', False))
        logo_item.connect('toggled', self._on_show_logo)
        self.menu.append(logo_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── Danger mode ───────────────────────────────────────────────────────
        danger_item = Gtk.MenuItem(label='Danger mode')
        danger_menu = Gtk.Menu()
        danger_item.set_submenu(danger_menu)

        bar_danger = Gtk.CheckMenuItem(label='Colour bar by usage')
        bar_danger.set_active(self.config.get('danger_bar', False))
        bar_danger.connect('toggled', self._on_danger_bar)
        danger_menu.append(bar_danger)

        text_danger = Gtk.CheckMenuItem(label='Colour text by usage')
        text_danger.set_active(self.config.get('danger_text', False))
        text_danger.connect('toggled', self._on_danger_text)
        danger_menu.append(text_danger)

        thres_item = Gtk.MenuItem(label='Set thresholds…')
        thres_item.connect('activate', self._on_thresholds)
        danger_menu.append(thres_item)

        danger_menu.append(Gtk.SeparatorMenuItem())
        hint = Gtk.MenuItem(label='Colours: green → yellow → orange → red')
        hint.set_sensitive(False)
        danger_menu.append(hint)

        self.menu.append(danger_item)

        # ── Bar colour ────────────────────────────────────────────────────────
        self._bar_colour_item = Gtk.MenuItem(label='Bar colour…')
        self._bar_colour_item.connect('activate', self._on_colour_pick)
        self._bar_colour_item.set_sensitive(not self.config.get('danger_bar', False))
        self.menu.append(self._bar_colour_item)

        self._track_colour_item = Gtk.MenuItem(label='Background colour…')
        self._track_colour_item.connect('activate', self._on_track_colour_pick)
        self.menu.append(self._track_colour_item)

        # ── Bar size ─────────────────────────────────────────────────────────
        size_item = Gtk.MenuItem(label='Bar size')
        size_menu = Gtk.Menu()
        size_item.set_submenu(size_menu)
        group = []
        for val, lbl in [('small', 'Small'), ('medium', 'Medium'),
                         ('large', 'Large'), ('xl', 'Extra large')]:
            ri = Gtk.RadioMenuItem.new_with_label(group, lbl)
            group = ri.get_group()
            ri.set_active(self.config.get('bar_size', 'medium') == val)
            ri.connect('toggled', self._on_bar_size, val)
            size_menu.append(ri)
        self.menu.append(size_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── Notify me when ────────────────────────────────────────────────────
        notify_item = Gtk.MenuItem(label='Notify me when…')
        notify_menu = Gtk.Menu()
        notify_item.set_submenu(notify_menu)
        group = []
        for val, lbl in [(0, 'Off'), (50, 'At 50% used'), (75, 'At 75% used'),
                         (90, 'At 90% used'), (95, 'At 95% used')]:
            ri = Gtk.RadioMenuItem.new_with_label(group, lbl)
            group = ri.get_group()
            ri.set_active(self.config.get('notify_at_pct', 0) == val)
            ri.connect('toggled', self._on_notify_pct, val)
            notify_menu.append(ri)
        self.menu.append(notify_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── Refresh / Quit ────────────────────────────────────────────────────
        refresh_item = Gtk.MenuItem(label='Refresh now')
        refresh_item.connect('activate', lambda _: self._schedule_fetch())
        self.menu.append(refresh_item)

        quit_item = Gtk.MenuItem(label='Quit')
        quit_item.connect('activate', lambda _: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()

    # ── Menu callbacks ─────────────────────────────────────────────────────────

    def _on_display_mode(self, item, val):
        if not item.get_active():
            return
        self.config['display_mode'] = val
        self._save_config()
        if self._usage:
            self._apply_update(self._usage, None)

    def _on_show_value(self, item, val):
        if not item.get_active():
            return
        self.config['show_value'] = val
        self._save_config()
        if self._usage:
            self._apply_update(self._usage, None)

    def _on_show_type(self, item, val):
        if not item.get_active():
            return
        self.config['show_type'] = val
        self._save_config()
        self._schedule_fetch()   # re-fetch so the new bucket shows immediately

    def _on_label_side(self, item, val):
        if not item.get_active():
            return
        self.config['label_side'] = val
        self._save_config()
        if self._usage:
            self._apply_update(self._usage, None)

    def _on_panel_order(self, item, val):
        if not item.get_active():
            return
        self.config['panel_order'] = val
        self._save_config()
        self._apply_panel_order()

    def _on_show_logo(self, item):
        self.config['show_logo'] = item.get_active()
        self._save_config()
        if self._usage:
            self._apply_update(self._usage, None)

    def _on_danger_bar(self, item):
        self.config['danger_bar'] = item.get_active()
        self._bar_colour_item.set_sensitive(not item.get_active())
        self._save_config()
        if self._usage:
            self._apply_update(self._usage, None)

    def _on_danger_text(self, item):
        self.config['danger_text'] = item.get_active()
        self._save_config()
        if self._usage:
            self._apply_update(self._usage, None)

    def _on_thresholds(self, _):
        """Open a simple dialog to configure danger-mode thresholds."""
        dlg = Gtk.Dialog(title='Danger thresholds (% used)')
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK,     Gtk.ResponseType.OK)
        dlg.set_default_size(300, -1)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        def _add_row(label, key):
            row  = Gtk.Box(spacing=8)
            lbl  = Gtk.Label(label=label, xalign=0)
            lbl.set_width_chars(24)
            adj  = Gtk.Adjustment(value=self.config[key], lower=1, upper=100,
                                  step_increment=1, page_increment=5)
            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
            row.pack_start(lbl,  True,  True,  0)
            row.pack_end(spin,   False, False, 0)
            box.add(row)
            return spin

        y_spin = _add_row('Yellow above (% used):', 'danger_yellow_pct')
        o_spin = _add_row('Orange above (% used):', 'danger_orange_pct')
        r_spin = _add_row('Red above (% used):',    'danger_red_pct')

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            self.config['danger_yellow_pct'] = int(y_spin.get_value())
            self.config['danger_orange_pct'] = int(o_spin.get_value())
            self.config['danger_red_pct']    = int(r_spin.get_value())
            self._save_config()
            if self._usage:
                self._apply_update(self._usage, None)
        dlg.destroy()

    def _on_colour_pick(self, _):
        dlg = Gtk.ColorChooserDialog(title='Bar colour', transient_for=None)
        rgba = Gdk.RGBA()
        rgba.parse(self.config['bar_color'])
        dlg.set_rgba(rgba)
        if dlg.run() == Gtk.ResponseType.OK:
            self.config['bar_color'] = '#{:02x}{:02x}{:02x}'.format(
                int(dlg.get_rgba().red   * 255),
                int(dlg.get_rgba().green * 255),
                int(dlg.get_rgba().blue  * 255),
            )
            self._save_config()
            if self._usage:
                self._apply_update(self._usage, None)
        dlg.destroy()

    def _on_track_colour_pick(self, _):
        dlg = Gtk.ColorChooserDialog(title='Background colour', transient_for=None)
        rgba = Gdk.RGBA()
        rgba.parse(self.config.get('track_color', '#464646'))
        dlg.set_rgba(rgba)
        if dlg.run() == Gtk.ResponseType.OK:
            self.config['track_color'] = '#{:02x}{:02x}{:02x}'.format(
                int(dlg.get_rgba().red   * 255),
                int(dlg.get_rgba().green * 255),
                int(dlg.get_rgba().blue  * 255),
            )
            self._save_config()
            if self._usage:
                self._apply_update(self._usage, None)
        dlg.destroy()

    def _on_bar_size(self, item, val):
        if not item.get_active():
            return
        self.config['bar_size'] = val
        self.config['bar_width'], self.config['bar_height'] = BAR_SIZE_PRESETS[val]
        self._save_config()
        if self._usage:
            self._apply_update(self._usage, None)

    def _on_notify_pct(self, item, val):
        if not item.get_active():
            return
        self.config['notify_at_pct'] = val
        self._notified_threshold = None   # reset so a new threshold fires immediately if already over
        self._save_config()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='Claude usage indicator for GNOME')
    p.add_argument('--debug', action='store_true',
                   help='Print raw API responses to stdout')
    args = p.parse_args()
    ClaudeStatus(debug=args.debug)
    Gtk.main()


if __name__ == '__main__':
    main()
