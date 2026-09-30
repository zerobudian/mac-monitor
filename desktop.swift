import Cocoa
import Foundation
import WebKit

private let compactSize = NSSize(width: 180, height: 180)
private let expandedSize = NSSize(width: 520, height: 390)

final class WindowDragView: NSView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .openHand)
    }

    override func mouseDown(with event: NSEvent) {
        if let window {
            NSCursor.closedHand.push()
            window.performDrag(with: event)
            NSCursor.pop()
        }
    }
}

final class DesktopMonitorApp: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var effectView: NSVisualEffectView!
    private var dragView: WindowDragView!
    private var dragHeightConstraint: NSLayoutConstraint!
    private var toggleButton: NSButton!
    private var pageURL: URL!

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard CommandLine.arguments.count > 1,
              let pageURL = URL(string: CommandLine.arguments[1]) else {
            NSApp.terminate(nil)
            return
        }
        self.pageURL = pageURL

        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "windowControl")
        configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")

        webView = WKWebView(frame: NSRect(origin: .zero, size: compactSize), configuration: configuration)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        webView.translatesAutoresizingMaskIntoConstraints = false

        effectView = NSVisualEffectView(frame: NSRect(origin: .zero, size: compactSize))
        effectView.material = .underWindowBackground
        effectView.blendingMode = .behindWindow
        effectView.state = .active
        effectView.wantsLayer = true
        effectView.layer?.cornerRadius = 26
        effectView.layer?.cornerCurve = .continuous
        effectView.layer?.masksToBounds = true
        effectView.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: effectView.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: effectView.trailingAnchor),
            webView.topAnchor.constraint(equalTo: effectView.topAnchor),
            webView.bottomAnchor.constraint(equalTo: effectView.bottomAnchor),
        ])
        installDragRegion()
        installGlassControls()

        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let origin = NSPoint(
            x: visible.maxX - compactSize.width - 22,
            y: visible.maxY - compactSize.height - 22
        )
        window = NSWindow(
            contentRect: NSRect(origin: origin, size: compactSize),
            styleMask: [.borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.contentView = effectView
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.isMovableByWindowBackground = true
        window.hidesOnDeactivate = false
        window.isReleasedWhenClosed = false
        window.acceptsMouseMovedEvents = true

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        webView.load(URLRequest(url: pageURL))
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard message.name == "windowControl", let command = message.body as? String else { return }
        switch command {
        case "expand":
            dragHeightConstraint.constant = 40
            resizeWindow(to: expandedSize, radius: 28)
            setToggleSymbol("arrow.down.right.and.arrow.up.left", help: "收起")
        case "compact":
            dragHeightConstraint.constant = 30
            resizeWindow(to: compactSize, radius: 26)
            setToggleSymbol("arrow.up.left.and.arrow.down.right", help: "展开")
        case "close":
            NSApp.terminate(nil)
        default:
            break
        }
    }

    private func installGlassControls() {
        toggleButton = symbolButton(
            "arrow.up.left.and.arrow.down.right",
            help: "展开",
            action: #selector(toggleWindow)
        )
        let exportButton = symbolButton(
            "square.and.arrow.down",
            help: "导出监控日志",
            action: #selector(exportLog)
        )
        let closeButton = symbolButton(
            "xmark",
            help: "退出监控",
            action: #selector(closeWindow)
        )
        let stack = NSStackView(views: [toggleButton, exportButton, closeButton])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 2
        stack.edgeInsets = NSEdgeInsets(top: 4, left: 5, bottom: 4, right: 5)

        let glass = NSGlassEffectView()
        glass.contentView = stack
        glass.cornerRadius = 15
        glass.wantsLayer = true
        glass.layer?.cornerCurve = .continuous
        glass.translatesAutoresizingMaskIntoConstraints = false
        effectView.addSubview(glass, positioned: .above, relativeTo: dragView)
        NSLayoutConstraint.activate([
            glass.topAnchor.constraint(equalTo: effectView.topAnchor, constant: 8),
            glass.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -10),
        ])
    }

    private func installDragRegion() {
        dragView = WindowDragView()
        dragView.translatesAutoresizingMaskIntoConstraints = false
        effectView.addSubview(dragView, positioned: .above, relativeTo: webView)
        dragHeightConstraint = dragView.heightAnchor.constraint(equalToConstant: 30)
        NSLayoutConstraint.activate([
            dragView.leadingAnchor.constraint(equalTo: effectView.leadingAnchor),
            dragView.trailingAnchor.constraint(equalTo: effectView.trailingAnchor),
            dragView.topAnchor.constraint(equalTo: effectView.topAnchor),
            dragHeightConstraint,
        ])
    }

    private func symbolButton(_ name: String, help: String, action: Selector) -> NSButton {
        let image = NSImage(systemSymbolName: name, accessibilityDescription: help) ?? NSImage()
        let button = NSButton(image: image, target: self, action: action)
        button.isBordered = false
        button.bezelStyle = .accessoryBarAction
        button.contentTintColor = .labelColor
        button.toolTip = help
        button.setAccessibilityLabel(help)
        return button
    }

    private func setToggleSymbol(_ name: String, help: String) {
        toggleButton.image = NSImage(systemSymbolName: name, accessibilityDescription: help)
        toggleButton.toolTip = help
        toggleButton.setAccessibilityLabel(help)
    }

    @objc private func toggleWindow() {
        let element = window.frame.size.width > compactSize.width + 20 ? "compactBtn" : "expandBtn"
        webView.evaluateJavaScript("document.getElementById('\(element)').click()")
    }

    @objc private func exportLog() {
        guard var parts = URLComponents(url: pageURL, resolvingAgainstBaseURL: false) else { return }
        parts.path = "/api/export"
        parts.query = nil
        if let url = parts.url { exportCSV(from: url) }
    }

    @objc private func closeWindow() {
        NSApp.terminate(nil)
    }

    private func resizeWindow(to size: NSSize, radius: CGFloat) {
        guard let screen = window.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        let current = window.frame
        let right = min(current.maxX, visible.maxX - 12)
        let top = min(current.maxY, visible.maxY - 12)
        var next = NSRect(
            x: right - size.width,
            y: top - size.height,
            width: size.width,
            height: size.height
        )
        if next.minX < visible.minX + 12 { next.origin.x = visible.minX + 12 }
        if next.minY < visible.minY + 12 { next.origin.y = visible.minY + 12 }
        effectView.layer?.cornerRadius = radius
        window.setFrame(next, display: true, animate: true)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard navigationAction.navigationType == .linkActivated,
              let url = navigationAction.request.url,
              url.path == "/api/export" else {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        exportCSV(from: url)
    }

    private func exportCSV(from url: URL) {
        let panel = NSSavePanel()
        panel.title = "导出监控日志"
        panel.nameFieldStringValue = "mac-monitor-\(fileTimestamp()).csv"
        panel.allowedContentTypes = [.commaSeparatedText]
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { response in
            guard response == .OK, let destination = panel.url else { return }
            URLSession.shared.dataTask(with: url) { data, _, _ in
                guard let data = data else { return }
                try? data.write(to: destination, options: .atomic)
            }.resume()
        }
    }

    private func fileTimestamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter.string(from: Date())
    }
}

let application = NSApplication.shared
let delegate = DesktopMonitorApp()
application.setActivationPolicy(.accessory)
application.delegate = delegate
application.run()
