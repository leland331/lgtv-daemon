"""Tray icon: StatusNotifierItem + com.canonical.dbusmenu over Gio (no extra dependencies)."""
import os
import sys

from gi.repository import Gio, GLib

from .keepawake import other_blockers

APP_ID = "io.github.lgtv_daemon"
ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"

ITEM_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="WindowId" type="i" access="read"/><property name="IconName" type="s" access="read"/>
<property name="IconThemePath" type="s" access="read"/><property name="OverlayIconName" type="s" access="read"/>
<property name="AttentionIconName" type="s" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/><property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewToolTip"/>
<signal name="NewStatus"><arg type="s"/></signal>
</interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
<arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
<arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
<arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ia{sv})"/></signal>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>"""


def run_once(fn):
    """GLib idle callback that runs fn once."""
    fn()
    return False


def sv(v):
    return GLib.Variant("s", v) if isinstance(v, str) else GLib.Variant("b" if isinstance(v, bool) else "i", v)


class Tray:
    def __init__(self, daemon):
        self.d = daemon
        self.conn = None
        self.name = None
        self.owner_id = 0
        self.watch_id = 0
        self.regs = []
        self.registered = False
        self.rev = 1
        self.other = ""       # someone else blocking idle (not us)
        self.poll_id = 0
        self.items = [   # (id, label, action)  action None = separator
            (1, "Open LGTV Daemon", self.d.open_gui), (2, None, None),
            (3, "Keep Screen On", self.d.toggle_keep_awake), (4, None, None),
            (5, "Turn TV On", lambda: self.d.manual_on()), (6, "Turn TV Off", lambda: self.d.manual_off()),
            (7, "Blank Screen", lambda: self.d.on_idle(forced=True)), (8, None, None),
            (9, "Quit Tray", self.d.quit_tray)]

    @property
    def active(self):
        return self.conn is not None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.conn:
            return
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            item = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self.regs = [self.conn.register_object(ITEM_PATH, item, self._item_call, self._item_get, None),
                         self.conn.register_object(MENU_PATH, menu, self._menu_call, self._menu_get, None)]
        except GLib.Error as e:
            self.d.log("tray unavailable: %s" % e.message)
            self.stop()
            return
        self.name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
        self.owner_id = Gio.bus_own_name_on_connection(self.conn, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        self.other = other_blockers()
        self.poll_id = GLib.timeout_add_seconds(15, self._poll)
        self.watch_id = Gio.bus_watch_name_on_connection(
            self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_up, self._watcher_gone)

    def stop(self):
        if self.poll_id:
            GLib.source_remove(self.poll_id)
            self.poll_id = 0
        if self.watch_id:
            Gio.bus_unwatch_name(self.watch_id)
        if self.owner_id:
            Gio.bus_unown_name(self.owner_id)   # the watcher drops the item when the name goes away
        for r in self.regs:
            self.conn.unregister_object(r)
        self.conn, self.name, self.owner_id, self.watch_id, self.regs = None, None, 0, 0, []
        self.registered = False

    def _watcher_up(self, conn, name, owner):
        try:
            conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, 3000, None, self._registered)
        except GLib.Error as e:
            self.d.log("tray register failed: %s" % e.message)

    def _registered(self, conn, res):
        try:
            conn.call_finish(res)
            self.registered = True
            self.d.log("tray icon registered")
        except GLib.Error as e:
            self.d.log("tray register failed: %s" % e.message)

    def _watcher_gone(self, conn, name):
        self.registered = False
        self.d.log("no StatusNotifierWatcher (on GNOME install the AppIndicator extension); tray icon waits for one")

    # ------------------------------------------------------------ state
    def _poll(self):
        other = other_blockers()
        if other != self.other:
            self.other = other
            self.refresh()
        return True

    def refresh(self, recheck=False):
        """Keep Screen On or another blocker changed: update icon, tooltip and the menu check."""
        if not self.conn:
            return
        if recheck:
            self.other = other_blockers()
        for s in ("NewIcon", "NewToolTip"):
            self.conn.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", s, None)
        self.rev += 1
        self.conn.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                              GLib.Variant("(ui)", (self.rev, 0)))

    # ------------------------------------------------------------ StatusNotifierItem
    def _item_get(self, conn, sender, path, iface, prop):
        if prop == "ToolTip":
            if self.d.keep.active:
                text = "Screen off and locking blocked"
            elif self.other:
                text = "Screen off and locking blocked by " + self.other
            else:
                text = "Screen off and locking allowed"
            return GLib.Variant("(sa(iiay)ss)", (self._icon(), [], "LGTV Daemon", text))
        return {"Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", APP_ID),
                "Title": GLib.Variant("s", "LGTV Daemon"), "Status": GLib.Variant("s", "Active"),
                "WindowId": GLib.Variant("i", 0), "IconName": GLib.Variant("s", self._icon()),
                "IconThemePath": GLib.Variant("s", ""), "OverlayIconName": GLib.Variant("s", ""),
                "AttentionIconName": GLib.Variant("s", ""), "ItemIsMenu": GLib.Variant("b", False),
                "Menu": GLib.Variant("o", MENU_PATH)}.get(prop)

    def _icon(self):
        return APP_ID + ("-awake" if self.d.keep.active or self.other else "")

    def _item_call(self, conn, sender, path, iface, method, params, inv):
        if method == "Activate":
            self.d.open_gui()
        elif method == "SecondaryActivate":
            self.d.toggle_keep_awake()
        inv.return_value(None)

    # ------------------------------------------------------------ dbusmenu
    def _props(self, item):
        i, label, _ = item
        if label is None:
            return {"type": sv("separator")}
        p = {"label": sv(label), "enabled": sv(True), "visible": sv(True)}
        if i == 3:
            p.update({"toggle-type": sv("checkmark"), "toggle-state": sv(1 if self.d.keep.active else 0)})
        return p

    def _node(self, item):
        return (item[0], self._props(item), [])

    def _menu_get(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _menu_call(self, conn, sender, path, iface, method, params, inv):
        a = params.unpack() if params is not None else ()
        if method == "GetLayout":
            if a[0] == 0:
                kids = [GLib.Variant("(ia{sv}av)", self._node(it)) for it in self.items]
                root = (0, {"children-display": sv("submenu")}, kids)
            else:
                root = next((self._node(it) for it in self.items if it[0] == a[0]), (a[0], {}, []))
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (self.rev, root)))
        elif method == "GetGroupProperties":
            ids = a[0] or [it[0] for it in self.items]
            inv.return_value(GLib.Variant("(a(ia{sv}))", ([(it[0], self._props(it)) for it in self.items
                                                          if it[0] in ids],)))
        elif method == "GetProperty":
            it = next((x for x in self.items if x[0] == a[0]), None)
            p = self._props(it).get(a[1]) if it else None
            if p is None:
                inv.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "no such property")
            else:
                inv.return_value(GLib.Variant("(v)", (p,)))
        elif method == "Event":
            self._event(a[0], a[1])
            inv.return_value(None)
        elif method == "EventGroup":
            for i, e, _, _ in a[0]:
                self._event(i, e)
            inv.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))

    def _event(self, i, event):
        if event == "clicked":
            for it in self.items:
                if it[0] == i and it[2]:
                    GLib.idle_add(run_once, it[2])
