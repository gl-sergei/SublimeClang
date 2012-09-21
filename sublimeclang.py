"""
Copyright (c) 2011-2012 Fredrik Ehnbom

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

   1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.

   2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.

   3. This notice may not be removed or altered from any source
   distribution.
"""
try:
    import sublime
    import ctypes
except:
    sublime.error_message("""\
Unfortunately ctypes can't be imported, so SublimeClang will not work.

There is a work around for this to get it to work, \
please see http://www.github.com/quarnster/SublimeClang for more details. """)

from clang import cindex
import sublime_plugin
from sublime import Region
import sublime
import os
import re
import threading
import time
from errormarkers import clear_error_marks, add_error_mark, show_error_marks, \
                         update_statusbar, erase_error_marks, clang_error_panel
from common import get_setting, get_settings, is_supported_language, get_language, get_cpu_count, run_in_main_thread, status_message
import translationunitcache
from parsehelp import parsehelp
import Queue


def warm_up_cache(view, filename=None):
    if filename == None:
        filename = view.file_name()
    stat = translationunitcache.tuCache.get_status(filename)
    if stat == translationunitcache.TranslationUnitCache.STATUS_NOT_IN_CACHE:
        translationunitcache.tuCache.add(view, filename)
    return stat


def get_translation_unit(view, filename=None, blocking=False):
    if filename == None:
        filename = view.file_name()
    if get_setting("warm_up_in_separate_thread", True, view) and not blocking:
        stat = warm_up_cache(view, filename)
        if stat == translationunitcache.TranslationUnitCache.STATUS_NOT_IN_CACHE:
            return None
        elif stat == translationunitcache.TranslationUnitCache.STATUS_PARSING:
            sublime.status_message("Hold your horses, cache still warming up")
            return None
    return translationunitcache.tuCache.get_translation_unit(filename, translationunitcache.tuCache.get_opts(view), translationunitcache.tuCache.get_opts_script(view))

navigation_stack = []
clang_complete_enabled = True
clang_fast_completions = True


class ClangTogglePanel(sublime_plugin.WindowCommand):
    def run(self, **args):
        show = args["show"] if "show" in args else None
        aview = sublime.active_window().active_view()
        error_marks = get_setting("error_marks_on_panel_only", False, aview)

        if show or (show == None and not clang_error_panel.is_visible(self.window)):
            clang_error_panel.open(self.window)
            if error_marks:
                show_error_marks(aview)
        else:
            clang_error_panel.close()
            if error_marks:
                erase_error_marks(aview)


class ClangToggleCompleteEnabled(sublime_plugin.TextCommand):
    def run(self, edit):
        global clang_complete_enabled
        clang_complete_enabled = not clang_complete_enabled
        sublime.status_message("Clang complete is %s" % ("On" if clang_complete_enabled else "Off"))


class ClangToggleFastCompletions(sublime_plugin.TextCommand):
    def run(self, edit):
        global clang_fast_completions
        clang_fast_completions = not clang_fast_completions
        sublime.status_message("Clang fast completions are %s" % ("On" if clang_fast_completions else "Off"))


class ClangWarmupCache(sublime_plugin.TextCommand):
    def run(self, edit):
        stat = warm_up_cache(self.view)
        if stat == translationunitcache.TranslationUnitCache.STATUS_PARSING:
            sublime.status_message("Cache is already warming up")
        elif stat != translationunitcache.TranslationUnitCache.STATUS_NOT_IN_CACHE:
            sublime.status_message("Cache is already warmed up")


class ClangGoBackEventListener(sublime_plugin.EventListener):
    def on_close(self, view):
        if not get_setting("pop_on_close", True, view):
            return
        # If the view we just closed was last in the navigation_stack,
        # consider it "popped" from the stack
        fn = view.file_name()
        if fn == None:
            return
        while True:
            if len(navigation_stack) == 0 or \
                    not navigation_stack[
                        len(navigation_stack) - 1][1].startswith(fn):
                break
            navigation_stack.pop()


class ClangGoBack(sublime_plugin.TextCommand):
    def run(self, edit):
        if len(navigation_stack) > 0:
            self.view.window().open_file(
                navigation_stack.pop()[0], sublime.ENCODED_POSITION)

    def is_enabled(self):
        return is_supported_language(sublime.active_window().active_view()) and len(navigation_stack) > 0

    def is_visible(self):
        return is_supported_language(sublime.active_window().active_view())


def format_cursor(cursor):
    return "%s:%d:%d" % (cursor.location.file.name, cursor.location.line,
                         cursor.location.column)


def format_current_file(view):
    row, col = view.rowcol(view.sel()[0].a)
    return "%s:%d:%d" % (view.file_name(), row + 1, col + 1)


def dump_cursor(cursor):
    if cursor is None:
        print "None"
    else:
        print cursor.kind, cursor.displayname, cursor.spelling
        print format_cursor(cursor)


def open(view, target):
    navigation_stack.append((format_current_file(view), target))
    view.window().open_file(target, sublime.ENCODED_POSITION)


class ExtensiveSearch:

    def quickpanel_extensive_search(self, idx):
        if idx == 0:
            for cpu in range(get_cpu_count()):
                t = threading.Thread(target=self.worker)
                t.start()
            self.queue.put((0, "*/+", self.window.folders(), (translationunitcache.tuCache.get_opts(self.view), translationunitcache.tuCache.get_opts_script(self.view))))

    def __init__(self, cursor, spelling, view, window, name="", impl=True, search_re=None, file_re=None):
        self.name = name
        if impl:
            self.re = re.compile(r"(\w+\s+|\w+::|\*|&)(%s\s*\([^;\{]*\))\s*\{" % re.escape(spelling))
            self.impre = re.compile(r"(\.cpp|\.c|\.cc|\.m|\.mm)$")
        else:
            self.re = re.compile(r"(\w+\s+|\w+::|\*|&)(%s\s*\([^;\{}]*\))\s*;" % re.escape(spelling))
            self.impre = re.compile(r"(\.h|\.hpp)$")
        if search_re != None:
            self.re = search_re
        if file_re != None:
            self.impre = file_re
        self.impl = impl
        self.view = view
        self.target = ""
        self.cursor = cursor
        self.window = window
        self.queue = Queue.PriorityQueue()
        self.candidates = Queue.Queue()
        self.lock = threading.RLock()
        self.timer = None
        self.status_count = 0

        display = [["Yes", "Do extensive search"], ["No", "Don't do extensive search"]]
        self.window.show_quick_panel(display, self.quickpanel_extensive_search)


    def quickpanel_on_done(self, idx):
        if idx == -1:
            return
        open(self.view, self.selection[idx])

    def done(self):
        if len(self.target) > 0:
            open(self.view, self.target)
        elif not self.candidates.empty():
            display = []
            self.selection = []
            while not self.candidates.empty():
                name, function, line, column = self.candidates.get()
                pos = "%s:%d:%d" % (name, line, column)
                self.selection.append(pos)
                display.append([function, pos])
                self.candidates.task_done()
            self.window.show_quick_panel(display, self.quickpanel_on_done)
        else:
            sublime.status_message("Don't know where the %s is!" % ("implementation" if self.impl else "definition"))

    def do_message(self):
        try:
            self.lock.acquire()
            run_in_main_thread(lambda: status_message(self.status))
            self.status_count = 0
            self.timer = None
        finally:
            self.lock.release()

    def set_status(self, message):
        try:
            self.lock.acquire()
            self.status = message
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.status_count += 1
            if self.status_count == 30:
                self.do_message()
            else:
                self.timer = threading.Timer(0.1, self.do_message)
        finally:
            self.lock.release()

    def worker(self):
        try:
            while len(self.target) == 0:
                prio, name, opts, opts_script = self.queue.get(timeout=60)
                if name == "*/+":
                    run_in_main_thread(lambda: status_message("Searching for %s..." % ("implementation" if self.impl else "definition")))
                    name = os.path.basename(self.name)
                    folders = opts
                    opts, opts_script = opts_script
                    for folder in folders:
                        for dirpath, dirnames, filenames in os.walk(folder):
                            for filename in filenames:
                                if self.impre.search(filename) != None:
                                    score = 1000
                                    for i in range(min(len(filename), len(name))):
                                        if filename[i] == name[i]:
                                            score -= 1
                                        else:
                                            break
                                    self.queue.put((score, os.path.join(dirpath, filename), opts, opts_script))
                    for i in range(get_cpu_count()-1):
                        self.queue.put((1001, "*/+++", None, None))

                    self.queue.put((1010, "*/++", None, None))
                    self.queue.task_done()
                    continue
                elif name == "*/++":
                    run_in_main_thread(self.done)
                    break
                elif name == "*/+++":
                    self.queue.task_done()
                    break

                remove = translationunitcache.tuCache.get_status(name) == translationunitcache.TranslationUnitCache.STATUS_NOT_IN_CACHE
                fine_search = not remove

                self.set_status("Searching %s" % name)

                # try a regex search first
                f = file(name, "r")
                data = f.read()
                f.close()
                match = self.re.search(data)
                if match != None:
                    fine_search = True
                    line, column = parsehelp.get_line_and_column_from_offset(data, match.start())
                    self.candidates.put((name, "".join(match.groups()), line, column))

                if fine_search and self.cursor and self.impl:
                    tu2 = translationunitcache.tuCache.get_translation_unit(name, opts, opts_script)
                    if tu2 != None:
                        tu2.lock()
                        try:
                            cursor2 = cindex.Cursor.get(
                                    tu2.var, self.cursor.location.file.name,
                                    self.cursor.location.line,
                                    self.cursor.location.column)
                            if not cursor2 is None:
                                d = cursor2.get_definition()
                                if not d is None and cursor2 != d:
                                    self.target = format_cursor(d)
                                    run_in_main_thread(self.done)
                        finally:
                            tu2.unlock()
                        if remove:
                            translationunitcache.tuCache.remove(name)
                self.queue.task_done()
        except Queue.Empty as e:
            pass
        except:
            import traceback
            traceback.print_exc()

class ClangGotoImplementation(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        tu = get_translation_unit(view)
        if tu == None:
            return
        tu.lock()
        target = ""

        try:
            row, col = view.rowcol(view.sel()[0].a)
            cursor = cindex.Cursor.get(tu.var, view.file_name(),
                                       row + 1, col + 1)
            spelling = view.substr(view.word(view.sel()[0].a))
            if cursor is None or cursor.kind.is_invalid() or cursor.displayname != spelling:
                ExtensiveSearch(None, spelling, self.view, self.view.window())
                return
            d = cursor.get_definition()
            if not d is None and cursor != d:
                target = format_cursor(d)
            elif not d is None and cursor == d and \
                    (cursor.kind == cindex.CursorKind.VAR_DECL or \
                    cursor.kind == cindex.CursorKind.PARM_DECL or \
                    cursor.kind == cindex.CursorKind.FIELD_DECL):
                for child in cursor.get_children():
                    if child.kind == cindex.CursorKind.TYPE_REF:
                        d = child.get_definition()
                        if not d is None:
                            target = format_cursor(d)
                        break
            elif cursor.kind == cindex.CursorKind.CLASS_DECL:
                for child in cursor.get_children():
                    if child.kind == cindex.CursorKind.CXX_BASE_SPECIFIER:
                        d = child.get_definition()
                        if not d is None:
                            target = format_cursor(d)
            elif d is None:
                if cursor.kind == cindex.CursorKind.DECL_REF_EXPR or \
                        cursor.kind == cindex.CursorKind.MEMBER_REF_EXPR or \
                        cursor.kind == cindex.CursorKind.CALL_EXPR:
                    cursor = cursor.get_reference()

                if cursor.kind == cindex.CursorKind.CXX_METHOD or \
                        cursor.kind == cindex.CursorKind.FUNCTION_DECL or \
                        cursor.kind == cindex.CursorKind.CONSTRUCTOR or \
                        cursor.kind == cindex.CursorKind.DESTRUCTOR:
                    f = cursor.location.file.name
                    if f.endswith(".h"):
                        endings = ["cpp", "c", "cc", "m", "mm"]
                        for ending in endings:
                            f = "%s.%s" % (f[:f.rfind(".")], ending)
                            if f != view.file_name() and os.access(f, os.R_OK):
                                tu2 = get_translation_unit(view, f, True)
                                if tu2 == None:
                                    continue
                                tu2.lock()
                                try:
                                    cursor2 = cindex.Cursor.get(
                                            tu2.var, cursor.location.file.name,
                                            cursor.location.line,
                                            cursor.location.column)
                                    if not cursor2 is None:
                                        d = cursor2.get_definition()
                                        if not d is None and cursor2 != d:
                                            target = format_cursor(d)
                                            break
                                finally:
                                    tu2.unlock()
                        if len(target) == 0:
                            ExtensiveSearch(cursor, cursor.spelling, self.view, self.view.window(), cursor.location.file.name)
                            return
        finally:
            tu.unlock()
        if len(target) > 0:
            open(self.view, target)
        else:
            sublime.status_message("Don't know where the implementation is!")

    def is_enabled(self):
        return is_supported_language(sublime.active_window().active_view())

    def is_visible(self):
        return is_supported_language(sublime.active_window().active_view())


class ClangGotoDef(sublime_plugin.TextCommand):
    def quickpanel_on_done(self, idx):
        if idx == -1:
            return
        open(self.view, format_cursor(self.o[idx]))

    def quickpanel_format(self, cursor):
        return ["%s::%s" % (cursor.get_semantic_parent().spelling,
                            cursor.displayname), format_cursor(cursor)]

    def run(self, edit):
        view = self.view
        tu = get_translation_unit(view)
        if tu == None:
            return
        tu.lock()
        target = ""
        try:
            row, col = view.rowcol(view.sel()[0].a)
            cursor = cindex.Cursor.get(tu.var, view.file_name(),
                                       row + 1, col + 1)

            word = view.word(view.sel()[0].a)
            spelling = view.substr(word)
            if cursor is None or cursor.kind.is_invalid() or (cursor.displayname != spelling and cursor.kind != cindex.CursorKind.INCLUSION_DIRECTIVE):
                # Try to determine what we're supposed to be looking for
                data = view.substr(sublime.Region(0, view.line(view.sel()[0].a).end()))
                chars = r"[\[\]\(\)&|.+-/*,<>;]"
                for match in re.finditer(r"(^|\w+|=|%s|\s)\s*(%s)\s*($|==|%s)" % (chars, spelling, chars), data):
                    if (match.start(2), match.end(2)) == (word.begin(), word.end()):
                        if match.group(3) == "(":
                            # Probably a function
                            ExtensiveSearch(None, spelling, self.view, self.view.window(), impl=False)
                        else:
                            # A variable perhaps?
                            data = data[:match.end(2)] + "."
                            typedef = parsehelp.get_type_definition(data)
                            if typedef:
                                line, column, name, var, extra = typedef
                                if line > 0 and column > 0:
                                    open(view, "%s:%d:%d" % (view.file_name(), line, column))
                                elif name != None and name == parsehelp.get_base_type(name):
                                    search_re = re.compile(r"(^|\s|\})\s*(class|struct)(\s+%s\s*)(;|\{)" % name)
                                    ExtensiveSearch(None, name, self.view, self.view.window(), impl=False, search_re=search_re)
                        break
                return
            ref = cursor.get_reference()
            target = ""

            if not ref is None and cursor == ref:
                can = cursor.get_canonical_cursor()
                if not can is None and can != cursor:
                    target = format_cursor(can)
                else:
                    o = cursor.get_overridden()
                    if len(o) == 1:
                        target = format_cursor(o[0])
                    elif len(o) > 1:
                        self.o = o
                        opts = []
                        for i in range(len(o)):
                            opts.append(self.quickpanel_format(o[i]))
                        view.window().show_quick_panel(opts,
                                                       self.quickpanel_on_done)
                    elif (cursor.kind == cindex.CursorKind.VAR_DECL or \
                            cursor.kind == cindex.CursorKind.PARM_DECL or \
                            cursor.kind == cindex.CursorKind.FIELD_DECL):
                        for child in cursor.get_children():
                            if child.kind == cindex.CursorKind.TYPE_REF:
                                d = child.get_definition()
                                if not d is None:
                                    target = format_cursor(d)
                                break
                    elif cursor.kind == cindex.CursorKind.CLASS_DECL:
                        for child in cursor.get_children():
                            if child.kind == cindex.CursorKind.CXX_BASE_SPECIFIER:
                                d = child.get_definition()
                                if not d is None:
                                    target = format_cursor(d)
            elif not ref is None:
                target = format_cursor(ref)
            elif cursor.kind == cindex.CursorKind.INCLUSION_DIRECTIVE:
                f = cursor.get_included_file()
                if not f is None:
                    target = f.name
        finally:
            tu.unlock()
        if len(target) > 0:
            open(self.view, target)
        else:
            sublime.status_message("No parent to go to!")

    def is_enabled(self):
        return is_supported_language(sublime.active_window().active_view())

    def is_visible(self):
        return is_supported_language(sublime.active_window().active_view())


class ClangClearCache(sublime_plugin.TextCommand):
    def run(self, edit):
        translationunitcache.tuCache.clear()
        sublime.status_message("Cache cleared!")


class ClangReparse(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        unsaved_files = []
        if view.is_dirty():
            unsaved_files.append((view.file_name(),
                          view.substr(Region(0, view.size()))))
        translationunitcache.tuCache.reparse(view, view.file_name(), unsaved_files)


def ignore_diagnostic(path, ignoreDirs):
    normalized_path = os.path.abspath(os.path.normpath(os.path.normcase(path)))
    for d in ignoreDirs:
        if normalized_path.startswith(d):
            return True
    return False

def display_compilation_results(view):
    tu = get_translation_unit(view)
    errString = ""
    show = False
    clear_error_marks()  # clear visual error marks
    erase_error_marks(view)
    if tu == None:
        return

    if not tu.try_lock():
        return
    errorCount = 0
    warningCount = 0
    ignoreDirs = [os.path.abspath(os.path.normpath(os.path.normcase(d))) for d in get_setting("diagnostic_ignore_dirs", [], view)]
    try:
        if len(tu.var.diagnostics):
            errString = ""
            for diag in tu.var.diagnostics:
                f = diag.location
                filename = ""
                if f.file != None:
                    filename = f.file.name

                if ignore_diagnostic(filename, ignoreDirs):
                    continue

                err = "%s:%d,%d - %s - %s" % (filename, f.line, f.column,
                                              diag.severityName,
                                              diag.spelling)
                try:
                    if len(diag.disable_option) > 0:
                        err = "%s [Disable with %s]" % (err, diag.disable_option)
                except AttributeError:
                    pass
                if diag.severity == cindex.Diagnostic.Fatal and \
                        "not found" in diag.spelling:
                    err = "%s\nDid you configure the include path used by clang properly?\n" \
                          "See http://github.com/quarnster/SublimeClang for more details on "\
                          "how to configure SublimeClang." % (err)
                errString = "%s%s\n" % (errString, err)
                if diag.severity == cindex.Diagnostic.Warning:
                    warningCount += 1
                elif diag.severity >= cindex.Diagnostic.Error:
                    errorCount += 1
                """
                for range in diag.ranges:
                    errString = "%s%s\n" % (errString, range)
                for fix in diag.fixits:
                    errString = "%s%s\n" % (errString, fix)
                """
                add_error_mark(
                    diag.severityName, filename, f.line - 1, diag.spelling)
            show = get_setting("show_output_panel", True, view)
    finally:
        tu.unlock()
    if (errorCount > 0 or warningCount > 0) and get_setting("show_status", True, view):
        statusString = "Clang Status: "
        if errorCount > 0:
            statusString = "%s%d Error%s" % (statusString, errorCount, "s" if errorCount != 1 else "")
        if warningCount > 0:
            statusString = "%s%s%d Warning%s" % (statusString, ", " if errorCount > 0 else "",
                                                 warningCount, "s" if warningCount != 1 else "")
        view.set_status("SublimeClang", statusString)
    else:
        view.erase_status("SublimeClang")
    window = view.window()
    clang_error_panel.set_data(errString)
    update_statusbar(view)
    if not get_setting("error_marks_on_panel_only", False, view):
        show_error_marks(view)
    if not window is None:
        if show:
            window.run_command("clang_toggle_panel", {"show": True})
        elif get_setting("hide_output_when_empty", False, view):
            if clang_error_panel.is_visible():
                window.run_command("clang_toggle_panel", {"show": False})

member_regex = re.compile("(([a-zA-Z_]+[0-9_]*)|([\)\]])+)((\.)|(->))$")


def is_member_completion(view, caret):
    line = view.substr(Region(view.line(caret).a, caret))
    if member_regex.search(line) != None:
        return True
    elif get_language(view).startswith("objc"):
        return re.search(r"\[[\s\w\]]+\s+$", line) != None
    return False


class ClangComplete(sublime_plugin.TextCommand):
    def run(self, edit, characters):
        for region in self.view.sel():
            self.view.insert(edit, region.end(), characters)
        caret = self.view.sel()[0].begin()
        line = self.view.substr(sublime.Region(self.view.word(caret-1).a, caret))
        if is_member_completion(self.view, caret) or line.endswith("::"):
            self.view.run_command("hide_auto_complete")
            sublime.set_timeout(self.delayed_complete, 1)

    def delayed_complete(self):
        self.view.run_command("auto_complete")


class SublimeClangAutoComplete(sublime_plugin.EventListener):
    def __init__(self):
        s = get_settings()
        s.clear_on_change("options")
        s.add_on_change("options", self.load_settings)
        self.load_settings()
        self.recompile_timer = None
        self.not_code_regex = re.compile("(string.)|(comment.)")

    def load_settings(self):
        translationunitcache.tuCache.clear()
        self.dont_complete_startswith = get_setting("dont_complete_startswith",
                                              ['operator', '~'])
        self.recompile_delay = get_setting("recompile_delay", 1000)
        self.cache_on_load = get_setting("cache_on_load", True)
        self.remove_on_close = get_setting("remove_on_close", True)
        self.time_completions = get_setting("time_completions", False)

    def is_member_kind(self, kind):
        return  kind == cindex.CursorKind.CXX_METHOD or \
                kind == cindex.CursorKind.FIELD_DECL or \
                kind == cindex.CursorKind.OBJC_PROPERTY_DECL or \
                kind == cindex.CursorKind.OBJC_CLASS_METHOD_DECL or \
                kind == cindex.CursorKind.OBJC_INSTANCE_METHOD_DECL or \
                kind == cindex.CursorKind.OBJC_IVAR_DECL or \
                kind == cindex.CursorKind.FUNCTION_TEMPLATE or \
                kind == cindex.CursorKind.NOT_IMPLEMENTED

    def return_completions(self, comp, view):
        if get_setting("inhibit_sublime_completions", True, view):
            return (comp, sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)
        return comp

    def on_query_completions(self, view, prefix, locations):
        global clang_complete_enabled
        if not is_supported_language(view) or not clang_complete_enabled:
            return []

        line = view.substr(sublime.Region(view.line(locations[0]).begin(), locations[0]))
        match = re.search(r"[,\s]*(\w+)\s+\w+$", line)
        if match != None:
            valid = ["new", "delete", "return", "goto", "case", "const", "static", "class", "struct", "typedef", "union"]
            if match.group(1) not in valid:
                # Probably a variable or function declaration
                # There's no point in trying to complete
                # a name that hasn't been typed yet...
                return self.return_completions([], view)

        timing = ""
        tot = 0
        start = time.time()
        tu = get_translation_unit(view)
        if tu == None:
            return self.return_completions([], view)
        ret = None
        tu.lock()
        try:
            if self.time_completions:
                curr = (time.time() - start)*1000
                tot += curr
                timing += "TU: %f" % (curr)
                start = time.time()

            cached_results = None
            if clang_fast_completions and get_setting("enable_fast_completions", True, view):
                data = view.substr(sublime.Region(0, locations[0]))
                cached_results = tu.cache.complete(data, prefix)
            if cached_results != None:
                print "found fast completions"
                ret = cached_results
            else:
                print "doing slow completions"
                row, col = view.rowcol(locations[0] - len(prefix))
                unsaved_files = []
                if view.is_dirty():
                    unsaved_files.append((view.file_name(),
                                      view.substr(Region(0, view.size()))))
                ret = tu.cache.clangcomplete(view.file_name(), row+1, col+1, unsaved_files, is_member_completion(view, locations[0] - len(prefix)))
            if self.time_completions:
                curr = (time.time() - start)*1000
                tot += curr
                timing += ", Comp: %f" % (curr)
                start = time.time()

            if len(self.dont_complete_startswith) and ret:
                i = 0
                while i < len(ret):
                    disp = ret[i][0]
                    pop = False
                    for comp in self.dont_complete_startswith:
                        if disp.startswith(comp):
                            pop = True
                            break

                    if pop:
                        ret.pop(i)
                    else:
                        i += 1

            if self.time_completions:
                curr = (time.time() - start)*1000
                tot += curr
                timing += ", Filter: %f" % (curr)
                timing += ", Tot: %f ms" % (tot)
                print timing
                sublime.status_message(timing)
        finally:
            tu.unlock()

        if not ret is None:
            return self.return_completions(ret, view)
        return self.return_completions([], view)

    def reparse_done(self):
        display_compilation_results(self.view)

    def restart_recompile_timer(self, timeout):
        if self.recompile_timer != None:
            self.recompile_timer.cancel()
        self.recompile_timer = threading.Timer(timeout, sublime.set_timeout,
                                               [self.recompile, 0])
        self.recompile_timer.start()

    def recompile(self):
        view = self.view
        unsaved_files = []
        if view.is_dirty():
            unsaved_files.append((view.file_name(),
                                  view.substr(Region(0, view.size()))))
        if not translationunitcache.tuCache.reparse(view, view.file_name(), unsaved_files,
                        self.reparse_done):

            # Already parsing so retry in a bit
            self.restart_recompile_timer(1)

    def on_activated(self, view):
        if is_supported_language(view) and get_setting("reparse_on_activated", True, view):
            self.view = view
            self.restart_recompile_timer(0.1)

    def on_post_save(self, view):
        if is_supported_language(view) and get_setting("reparse_on_save", True, view):
            self.view = view
            self.restart_recompile_timer(0.1)

    def on_modified(self, view):
        if (self.recompile_delay <= 0) or not is_supported_language(view):
            return

        self.view = view
        self.restart_recompile_timer(self.recompile_delay / 1000.0)

    def on_load(self, view):
        if self.cache_on_load and is_supported_language(view):
            warm_up_cache(view)

    def on_close(self, view):
        if self.remove_on_close and is_supported_language(view):
            translationunitcache.tuCache.remove(view.file_name())

    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "clang_supported_language":
            if view == None:
                view = sublime.active_window().active_view()
            return is_supported_language(view)
        elif key == "clang_is_code":
            return self.not_code_regex.search(view.scope_name(view.sel()[0].begin())) == None
        elif key == "clang_complete_enabled":
            return clang_complete_enabled
        elif key == "clang_automatic_completion_popup":
            return get_setting("automatic_completion_popup", True, view)
        elif key == "clang_panel_visible":
            return clang_error_panel.is_visible()
