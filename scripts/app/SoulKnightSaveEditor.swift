// Soul Knight Save Editor - native macOS front end.
//
// A normal AppKit application window that hosts the tool's interface and runs the
// Python command line as child processes, streaming their output into the page.
// There is no local web server and no browser: the sources live in
// ~/Library/Application Support/SoulKnightSaveEditor/tool, copied out of the app
// bundle on first launch so the bundle itself never has to be written to.

import Cocoa
import WebKit

let bundleTool = "tool"                       // Contents/Resources/tool
let supportName = "SoulKnightSaveEditor"

/// Append a line to ~/Library/Application Support/SoulKnightSaveEditor/app.log.
/// Launched from Finder the app has nowhere to print, and this is the first place
/// to look when the window does not appear.
func note(_ message: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(message)\n"
    let url = Host.supportDirectory().appendingPathComponent("app.log")
    let manager = FileManager.default
    if let handle = try? FileHandle(forWritingTo: url) {
        handle.seekToEndOfFile()
        handle.write(Data(line.utf8))
        try? handle.close()
    } else {
        try? line.write(to: url, atomically: true, encoding: .utf8)
    }
}

final class Host: NSObject, WKScriptMessageHandler, WKWebViewConfigurationProviding {
    var window: NSWindow!
    var webView: WKWebView!
    var tool: URL = URL(fileURLWithPath: NSTemporaryDirectory())
    var jobScript: Process?
    var jobName = ""
    var snapshotPath: String?
    var state: [String: Any]?

    // ------------------------------------------------------------------ setup
    func start() {
        note("starting, bundle=\(Bundle.main.bundlePath)")
        tool = Self.prepareToolDirectory()
        note("tool directory=\(tool.path)")
        note("creating the web view")
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "sksave")
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1180, height: 820),
                            configuration: configuration)
        note("web view ready")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "元气骑士 存档修改器"
        window.minSize = NSSize(width: 980, height: 660)
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.setFrameAutosaveName("SKSaveMainWindow")
        note("window created, visible=\(window.isVisible) frame=\(NSStringFromRect(window.frame))")

        loadInterface()

        if let path = snapshotPath {
            // --snapshot <file>: let the page finish its round trips, save the window
            // as a PNG and quit.  Used to check the layout and to make README images.
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                guard let self = self, let view = self.webView else { return }
                view.takeSnapshot(with: nil) { image, error in
                    if let image = image,
                       let tiff = image.tiffRepresentation,
                       let bitmap = NSBitmapImageRep(data: tiff),
                       let png = bitmap.representation(using: .png, properties: [:]) {
                        try? png.write(to: URL(fileURLWithPath: path))
                        note("snapshot written to \(path)")
                    } else {
                        note("snapshot failed: \(error?.localizedDescription ?? "unknown")")
                    }
                    NSApp.terminate(nil)
                }
            }
        }
    }

    func loadInterface() {
        let page = tool.appendingPathComponent("sksave/ui.html")
        if let html = try? String(contentsOf: page, encoding: .utf8) {
            note("loading interface, \(html.count) bytes")
            webView.loadHTMLString(html, baseURL: nil)
        } else {
            note("ERROR: cannot read \(page.path)")
            webView.loadHTMLString("<h1>missing sksave/ui.html</h1>", baseURL: nil)
        }
    }

    // ------------------------------------------------- first launch: copy tool
    static func supportDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let directory = base.appendingPathComponent(supportName, isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    static func prepareToolDirectory() -> URL {
        let support = supportDirectory()
        let destination = support.appendingPathComponent(bundleTool, isDirectory: true)
        let bundled = Bundle.main.resourceURL?.appendingPathComponent(bundleTool, isDirectory: true)
        guard let source = bundled, FileManager.default.fileExists(atPath: source.path) else {
            return destination
        }
        // Re-sync whenever the installed copy is missing, incomplete or simply different:
        // the version alone would hide every change made between releases.
        let marker = destination.appendingPathComponent(".bundled-version")
        let version = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0"
        let fingerprint = version + " " + ["sksave/ui.html", "sksave/patcher.py", "run.py"]
            .map { relative -> String in
                let attributes = try? FileManager.default.attributesOfItem(
                    atPath: source.appendingPathComponent(relative).path)
                return "\(relative):\((attributes?[.size] as? Int) ?? 0)"
            }
            .joined(separator: ",")
        let installed = (try? String(contentsOf: marker, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if installed != fingerprint {
            note("syncing the tool directory from the bundle")
            copyTree(from: source, to: destination)
            try? fingerprint.write(to: marker, atomically: true, encoding: .utf8)
        }
        return destination
    }

    static func copyTree(from source: URL, to destination: URL) {
        let manager = FileManager.default
        let skip: Set<String> = [".venv", "work", "vendor", "__pycache__", ".git"]
        guard let entries = try? manager.contentsOfDirectory(at: source,
                                                             includingPropertiesForKeys: nil) else { return }
        try? manager.createDirectory(at: destination, withIntermediateDirectories: true)
        for entry in entries where !skip.contains(entry.lastPathComponent) {
            let target = destination.appendingPathComponent(entry.lastPathComponent)
            if (try? entry.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true {
                copyTree(from: entry, to: target)
            } else {
                try? manager.removeItem(at: target)
                try? manager.copyItem(at: entry, to: target)
            }
        }
    }

    // ------------------------------------------------------------- interpreters
    var venvPython: URL { tool.appendingPathComponent(".venv/bin/python") }

    func pythonFor(_ needDependencies: Bool) -> URL {
        if FileManager.default.isExecutableFile(atPath: venvPython.path) { return venvPython }
        return URL(fileURLWithPath: "/usr/bin/env")
    }

    func processEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if FileManager.default.fileExists(atPath: "/Applications/Xcode.app/Contents/Developer") {
            environment["DEVELOPER_DIR"] = "/Applications/Xcode.app/Contents/Developer"
        }
        environment["PYTHONUNBUFFERED"] = "1"
        return environment
    }

    // --------------------------------------------------------------- messaging
    func userContentController(_ controller: WKUserContentController,
                              didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let action = body["action"] as? String else {
            note("page message without an action: \(message.body)")
            return
        }
        note("page asked for \(action)")
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.perform(action: action, body: body)
        }
    }

    func perform(action: String, body: [String: Any]) {
        switch action {
        case "status":   emitStatus()
        case "state":    emit("state", state ?? NSNull())
        case "backups":  emit("backups", backups())
        case "start":    start(name: body["name"] as? String ?? "", body: body)
        default:         break
        }
    }

    func emit(_ type: String, _ payload: Any) {
        let wrapper: [String: Any] = ["type": type, "payload": payload]
        guard let data = try? JSONSerialization.data(withJSONObject: wrapper),
              let json = String(data: data, encoding: .utf8) else { return }
        DispatchQueue.main.async { [weak self] in
            self?.webView.evaluateJavaScript("window.sksave && window.sksave.onEvent(\(json))",
                                             completionHandler: nil)
        }
    }

    // ------------------------------------------------------------------ status
    func emitStatus() {
        var payload: [String: Any] = [:]
        payload["environment"] = environmentStatus()
        payload["devices"] = devices()
        payload["options"] = ["heroes", "hero_levels", "skins", "pets", "skills", "weapons",
                              "weapon_skins", "evolution", "kill_effects", "mythic", "materials",
                              "season_coin", "gems", "repair_format"]
        payload["state"] = state ?? NSNull()
        payload["version"] = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? ""
        emit("status", payload)
    }

    func capture(_ executable: URL, _ arguments: [String], timeout: TimeInterval = 120) -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = processEnvironment()
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do { try process.run() } catch { return "" }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { usleep(40_000) }
        if process.isRunning { process.terminate() }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? ""
    }

    func environmentStatus() -> [String: Any] {
        let systemPython = URL(fileURLWithPath: "/usr/bin/env")
        let json = capture(systemPython, ["python3", "-m", "sksave.env", "--status"])
        if let data = json.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            return object
        }
        return ["venv": false, "dependencies": false, "airlift": false, "xcode": false, "ready": false]
    }

    func devices() -> [[String: Any]] {
        guard FileManager.default.isExecutableFile(atPath: venvPython.path) else { return [] }
        let script = """
        import json, sys
        sys.path.insert(0, \(quoted(tool.path)))
        from sksave import device
        print(json.dumps(device.list_devices()))
        """
        let output = capture(venvPython, ["-c", script], timeout: 60).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = output.data(using: .utf8),
              let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return list
    }

    func quoted(_ value: String) -> String {
        let escaped = value.replacingOccurrences(of: "\\", with: "\\\\")
                           .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }

    // ---------------------------------------------------------------- backups
    func backups() -> [String: Any] {
        var result: [String: Any] = ["local": [], "device": []]
        var local: [String] = []
        let directory = tool.appendingPathComponent("work/backups", isDirectory: true)
        if let entries = try? FileManager.default.contentsOfDirectory(at: directory,
                                                                     includingPropertiesForKeys: [.isDirectoryKey]) {
            local = entries.filter { (try? $0.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true }
                           .map { $0.path }.sorted()
        }
        result["local"] = local
        guard FileManager.default.isExecutableFile(atPath: venvPython.path) else { return result }
        let report = tool.appendingPathComponent("work/native-restore-list.json")
        try? FileManager.default.removeItem(at: report)
        _ = capture(venvPython, [tool.appendingPathComponent("run.py").path,
                                 "--report", report.path, "restore", "--list"], timeout: 180)
        if let data = try? Data(contentsOf: report),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let remote = object["device"] as? [String] {
            result["device"] = remote.sorted()
        }
        return result
    }

    // -------------------------------------------------------------------- jobs
    func start(name: String, body: [String: Any]) {
        if jobScript != nil { return }                       // one job at a time
        var arguments: [String]
        var executable = venvPython
        var reportPath: URL?
        if name == "setup" {
            executable = URL(fileURLWithPath: "/usr/bin/env")
            arguments = ["python3", "-m", "sksave.env", "--install"]
        } else {
            guard FileManager.default.isExecutableFile(atPath: venvPython.path) else {
                emit("job", ["name": name, "status": "failed", "seconds": 0,
                             "log": "the environment is not installed yet - press \"set up\" first\n"])
                return
            }
            reportPath = tool.appendingPathComponent("work/native-report.json")
            try? FileManager.default.removeItem(at: reportPath!)
            arguments = [tool.appendingPathComponent("run.py").path, "--report", reportPath!.path]
            arguments += argumentsFor(name: name, body: body)
        }

        jobName = name
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = processEnvironment()
        process.currentDirectoryURL = tool
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        jobScript = process

        emit("job", ["name": name, "status": "running", "seconds": 0, "log": ""])
        let started = Date()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            self?.emit("job", ["name": name, "status": "running",
                               "seconds": Int(Date().timeIntervalSince(started)), "log": text])
        }
        process.terminationHandler = { [weak self] finished in
            guard let self = self else { return }
            pipe.fileHandleForReading.readabilityHandler = nil
            let leftovers = pipe.fileHandleForReading.readDataToEndOfFile()
            let tail = String(data: leftovers, encoding: .utf8) ?? ""
            if let report = reportPath, let data = try? Data(contentsOf: report),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let after = object["state"] as? [String: Any] { self.state = after }
                else if let after = object["after"] as? [String: Any] { self.state = after }
            }
            let ok = finished.terminationStatus == 0
            var payload: [String: Any] = ["name": name, "status": ok ? "done" : "failed",
                                          "seconds": Int(Date().timeIntervalSince(started)),
                                          "log": tail]
            if !ok { payload["log"] = tail + "\n(kindly check the message above)\n" }
            self.jobScript = nil
            self.emit("job", payload)
            if let state = self.state { self.emit("state", state) }
        }
        do { try process.run() } catch {
            jobScript = nil
            emit("job", ["name": name, "status": "failed", "seconds": 0,
                         "log": "could not start: \(error.localizedDescription)\n"])
        }
    }

    func argumentsFor(name: String, body: [String: Any]) -> [String] {
        if name == "restore" {
            let source = body["source"] as? String ?? ""
            return body["kind"] as? String == "device"
                ? ["restore", "--from-device", source]
                : ["restore", "--from", source]
        }
        if name != "unlock" { return [name] }
        var arguments = ["unlock"]
        let options = body["options"] as? [String: Bool] ?? [:]
        for (key, enabled) in options where !enabled {
            arguments.append("--no-" + key.replacingOccurrences(of: "_", with: "-"))
        }
        for (key, flag) in [("gems", "--gems"), ("seasonCoin", "--season-coin"),
                            ("quantity", "--quantity"), ("heroLevel", "--hero-level")] {
            if let value = body[key] as? Int, value > 0 { arguments += [flag, String(value)] }
        }
        arguments += ["--label", "app"]
        return arguments
    }
}

protocol WKWebViewConfigurationProviding {}

// ---------------------------------------------------------------- application
final class AppDelegate: NSObject, NSApplicationDelegate {
    let host = Host()

    func applicationDidFinishLaunching(_ notification: Notification) {
        note("applicationDidFinishLaunching")
        let arguments = CommandLine.arguments
        if let index = arguments.firstIndex(of: "--snapshot"), index + 1 < arguments.count {
            host.snapshotPath = arguments[index + 1]
        }
        buildMenu()
        host.start()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 元气骑士存档修改器",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "打开工具文件夹", action: #selector(openToolFolder), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    @objc func openToolFolder() {
        NSWorkspace.shared.open(host.tool)
    }
}

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = AppDelegate()
application.delegate = delegate
note("entering run loop, delegate=\(String(describing: application.delegate))")
application.run()
