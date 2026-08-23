import Cocoa
import Foundation

private extension NSColor {
    static func trosa(_ hex: UInt32, alpha: CGFloat = 1) -> NSColor {
        NSColor(
            red: CGFloat((hex >> 16) & 0xff) / 255,
            green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255,
            alpha: alpha
        )
    }
}

private enum TrosaPalette {
    static let canvas = NSColor.trosa(0xF3F0E8)
    static let paper = NSColor.trosa(0xFAF8F2)
    static let raised = NSColor.trosa(0xFFFDF8)
    static let ink = NSColor.trosa(0x28251F)
    static let softInk = NSColor.trosa(0x625E55)
    static let mutedInk = NSColor.trosa(0x8E887C)
    static let line = NSColor.trosa(0x363026, alpha: 0.15)
    static let clay = NSColor.trosa(0xA85F45)
    static let claySoft = NSColor.trosa(0xEFE1D8)
    static let moss = NSColor.trosa(0x66705A)
    static let mossSoft = NSColor.trosa(0xE1E7DA)
    static let mist = NSColor.trosa(0x647B82)
    static let mistSoft = NSColor.trosa(0xDFE8E8)
    static let ochre = NSColor.trosa(0xA98145)
    static let danger = NSColor.trosa(0xA94E45)
}

struct TrosaConfig {
    let projectRoot: URL
    let region: String
    let instanceID: String
    let publicURL: String
    let remoteRoot: String
    let dataDir: String

    static func load(projectRoot: URL) -> TrosaConfig? {
        let envURL = projectRoot.appendingPathComponent("deploy/cloud/workbench.env")
        guard let text = try? String(contentsOf: envURL, encoding: .utf8) else { return nil }
        var values: [String: String] = [:]
        for rawLine in text.components(separatedBy: .newlines) {
            let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !line.isEmpty, !line.hasPrefix("#"), let separator = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<separator]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: separator)...]).trimmingCharacters(in: .whitespaces)
            if value.count >= 2, value.first == "\"", value.last == "\"" {
                value.removeFirst()
                value.removeLast()
            }
            values[key] = value
        }
        guard let region = values["TRADE_OS_ECS_REGION"],
              let instanceID = values["TRADE_OS_ECS_INSTANCE_ID"],
              !region.isEmpty, !instanceID.isEmpty else { return nil }
        return TrosaConfig(
            projectRoot: projectRoot,
            region: region,
            instanceID: instanceID,
            publicURL: values["TRADE_OS_PUBLIC_URL"] ?? "https://app.trosa.space",
            remoteRoot: values["TRADE_OS_REMOTE_ROOT"] ?? "/opt/trosa",
            dataDir: values["TRADE_OS_DATA_DIR"] ?? "/var/lib/trosa"
        )
    }
}

struct RemoteEntry {
    let name: String
    let kind: String
    let size: Int64
    let modified: String

    var isDirectory: Bool { kind == "directory" }
    var typeLabel: String { isDirectory ? "文件夹" : (kind == "symlink" ? "链接" : "文件") }
    var sizeLabel: String {
        if isDirectory { return "—" }
        let units = ["B", "KB", "MB", "GB", "TB"]
        var value = Double(size)
        var index = 0
        while value >= 1024, index < units.count - 1 {
            value /= 1024
            index += 1
        }
        return index == 0 ? "\(size) B" : String(format: "%.1f %@", value, units[index])
    }
}

final class CommandRunner {
    static let shared = CommandRunner()

    func run(executable: String, arguments: [String], directory: URL?, completion: @escaping (String) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = arguments
            process.standardOutput = pipe
            process.standardError = pipe
            if let directory {
                process.currentDirectoryURL = directory
            }
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            process.environment = environment

            do {
                try process.run()
                let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                process.waitUntilExit()
                let text = output.trimmingCharacters(in: .whitespacesAndNewlines)
                if process.terminationStatus == 0 {
                    completion(text.isEmpty ? "完成" : text)
                } else {
                    completion("命令失败（退出码 \(process.terminationStatus)）\n\(text)")
                }
            } catch {
                completion("无法执行命令：\(error.localizedDescription)")
            }
        }
    }
}

final class TrosaManagerViewController: NSViewController, NSTableViewDataSource, NSTableViewDelegate {
    private let defaultProjectRoot = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Desktop/Trosa", isDirectory: true)
    private var projectRoot: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Desktop/Trosa", isDirectory: true)
    private var currentRemotePath = "/var/lib/trade-os"
    private var entries: [RemoteEntry] = []

    private let outputView = NSTextView()
    private let outputScroll = NSScrollView()
    private var technicalLogHeight: NSLayoutConstraint?
    private let technicalLogToggle = NSButton(title: "显示技术记录", target: nil, action: nil)
    private let tabs = NSTabView()
    private let activityIcon = NSImageView()
    private let activityLabel = NSTextField(labelWithString: "准备检查服务器状态")
    private let activityDetailLabel = NSTextField(labelWithString: "")

    private let statusLabel = NSTextField(labelWithString: "正在检查服务器")
    private let statusDetailLabel = NSTextField(labelWithString: "网站、应用和安全连接正在检查中。")
    private let checkedAtLabel = NSTextField(labelWithString: "")
    private let websiteHealthLabel = NSTextField(labelWithString: "待检查")
    private let appHealthLabel = NSTextField(labelWithString: "待检查")
    private let tunnelHealthLabel = NSTextField(labelWithString: "待检查")
    private let releaseLabel = NSTextField(labelWithString: "正在读取")
    private let deployReleaseLabel = NSTextField(labelWithString: "正在读取")
    private let fileStatusLabel = NSTextField(labelWithString: "请选择一个位置，再点击“打开”读取服务器文件。")
    private let gitStatusLabel = NSTextField(labelWithString: "正在检查本机更新")
    private let backupScheduleLabel = NSTextField(labelWithString: "每天 03:30 自动备份到这台 Mac")
    private let projectLabel = NSTextField(labelWithString: "")
    private let pathField = NSTextField(string: "/var/lib/trade-os")
    private let tableView = NSTableView()
    private let backupLabel = NSTextField(labelWithString: "")
    private let backupListLabel = NSTextField(labelWithString: "")

    override func loadView() {
        let root = UserDefaults.standard.string(forKey: "trosa.projectRoot")
        projectRoot = root.map { URL(fileURLWithPath: $0, isDirectory: true) } ?? defaultProjectRoot

        let mainView = NSView()
        mainView.wantsLayer = true
        mainView.layer?.backgroundColor = TrosaPalette.canvas.cgColor
        view = mainView

        let header = makeHeader()
        tabs.translatesAutoresizingMaskIntoConstraints = false
        tabs.addTabViewItem(tabItem("概览", makeOverviewTab()))
        tabs.addTabViewItem(tabItem("文件与资料", makeFilesTab()))
        tabs.addTabViewItem(tabItem("更新网站", makeDeployTab()))
        tabs.addTabViewItem(tabItem("备份", makeBackupTab()))
        tabs.tabViewType = .topTabsBezelBorder

        let activityBar = makeActivityBar()
        let technicalHeader = makeTechnicalLogHeader()

        outputScroll.translatesAutoresizingMaskIntoConstraints = false
        outputScroll.hasVerticalScroller = true
        outputScroll.borderType = .bezelBorder
        outputView.isEditable = false
        outputView.isSelectable = true
        outputView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        outputView.textColor = TrosaPalette.ink
        outputView.backgroundColor = TrosaPalette.raised
        outputScroll.documentView = outputView
        outputScroll.isHidden = true
        technicalLogHeight = outputScroll.heightAnchor.constraint(equalToConstant: 0)
        technicalLogHeight?.isActive = true

        mainView.addSubview(header)
        mainView.addSubview(activityBar)
        mainView.addSubview(tabs)
        mainView.addSubview(technicalHeader)
        mainView.addSubview(outputScroll)
        NSLayoutConstraint.activate([
            header.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            header.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            header.topAnchor.constraint(equalTo: mainView.topAnchor, constant: 16),
            header.heightAnchor.constraint(equalToConstant: 54),
            activityBar.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            activityBar.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            activityBar.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 10),
            activityBar.heightAnchor.constraint(equalToConstant: 48),
            tabs.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            tabs.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            tabs.topAnchor.constraint(equalTo: activityBar.bottomAnchor, constant: 12),
            tabs.bottomAnchor.constraint(equalTo: technicalHeader.topAnchor, constant: -8),
            technicalHeader.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            technicalHeader.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            technicalHeader.heightAnchor.constraint(equalToConstant: 28),
            outputScroll.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            outputScroll.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            outputScroll.topAnchor.constraint(equalTo: technicalHeader.bottomAnchor, constant: 4),
            outputScroll.bottomAnchor.constraint(equalTo: mainView.bottomAnchor, constant: -16),
        ])
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        refreshOverview()
        refreshLocalBackups()
        refreshGitStatus(showOutput: false)
    }

    private func makeHeader() -> NSView {
        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        let title = NSTextField(labelWithString: "trosa 工作台")
        title.font = NSFont.systemFont(ofSize: 23, weight: .semibold)
        title.textColor = TrosaPalette.ink
        title.translatesAutoresizingMaskIntoConstraints = false
        projectLabel.font = NSFont.systemFont(ofSize: 12)
        projectLabel.textColor = TrosaPalette.softInk
        projectLabel.translatesAutoresizingMaskIntoConstraints = false
        updateProjectLabel()
        let text = NSStackView(views: [title, projectLabel])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 2
        let choose = button("切换项目位置", #selector(chooseProjectDirectory), compact: true)
        let site = primaryButton("打开 trosa", #selector(openWebsite))
        let row = NSStackView(views: [text, NSView(), choose, site])
        row.translatesAutoresizingMaskIntoConstraints = false
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 10
        container.addSubview(row)
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            row.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            row.topAnchor.constraint(equalTo: container.topAnchor),
            row.bottomAnchor.constraint(equalTo: container.bottomAnchor)
        ])
        return container
    }

    private func makeActivityBar() -> NSView {
        let container = makePanel(background: TrosaPalette.mistSoft, cornerRadius: 12)
        activityIcon.translatesAutoresizingMaskIntoConstraints = false
        activityIcon.image = NSImage(systemSymbolName: "circle.dashed", accessibilityDescription: "当前状态")
        activityIcon.contentTintColor = TrosaPalette.mist
        activityIcon.setContentHuggingPriority(.required, for: .horizontal)

        activityLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        activityLabel.textColor = TrosaPalette.ink
        activityDetailLabel.font = NSFont.systemFont(ofSize: 12)
        activityDetailLabel.textColor = TrosaPalette.softInk
        let text = NSStackView(views: [activityLabel, activityDetailLabel])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 1
        text.translatesAutoresizingMaskIntoConstraints = false
        let row = NSStackView(views: [activityIcon, text])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 10
        row.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(row)
        NSLayoutConstraint.activate([
            activityIcon.widthAnchor.constraint(equalToConstant: 20),
            activityIcon.heightAnchor.constraint(equalToConstant: 20),
            row.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 14),
            row.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -14),
            row.centerYAnchor.constraint(equalTo: container.centerYAnchor)
        ])
        return container
    }

    private func makeTechnicalLogHeader() -> NSView {
        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        technicalLogToggle.target = self
        technicalLogToggle.action = #selector(toggleTechnicalLog)
        technicalLogToggle.bezelStyle = .inline
        technicalLogToggle.font = NSFont.systemFont(ofSize: 12)
        technicalLogToggle.contentTintColor = TrosaPalette.softInk
        technicalLogToggle.translatesAutoresizingMaskIntoConstraints = false
        let hint = NSTextField(labelWithString: "正常维护时不需要看这里")
        hint.font = NSFont.systemFont(ofSize: 11)
        hint.textColor = TrosaPalette.mutedInk
        hint.translatesAutoresizingMaskIntoConstraints = false
        let row = NSStackView(views: [technicalLogToggle, hint, NSView()])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 6
        row.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(row)
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            row.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            row.centerYAnchor.constraint(equalTo: container.centerYAnchor)
        ])
        return container
    }

    private func makeOverviewTab() -> NSView {
        let view = tabContainer()
        let heading = pageHeading(
            kicker: "服务器状态",
            title: "一眼知道今天要不要处理",
            description: "系统会检查网站、trosa 应用、安全连接和备份。显示“运行正常”时，你不需要进行任何技术操作。"
        )

        statusLabel.font = NSFont.systemFont(ofSize: 23, weight: .semibold)
        statusLabel.textColor = TrosaPalette.ink
        statusDetailLabel.font = NSFont.systemFont(ofSize: 13)
        statusDetailLabel.textColor = TrosaPalette.softInk
        checkedAtLabel.font = NSFont.systemFont(ofSize: 11)
        checkedAtLabel.textColor = TrosaPalette.mutedInk
        let statusText = NSStackView(views: [statusLabel, statusDetailLabel, checkedAtLabel])
        statusText.orientation = .vertical
        statusText.alignment = .leading
        statusText.spacing = 4
        let refresh = primaryButton("立即检查", #selector(refreshOverview))
        let statusRow = NSStackView(views: [statusText, NSView(), refresh])
        statusRow.orientation = .horizontal
        statusRow.alignment = .centerY
        statusRow.spacing = 16

        let healthItems = NSStackView(views: [
            statusMetric(title: "网站", detail: "客户可打开的 trosa", symbol: "globe", value: websiteHealthLabel),
            statusMetric(title: "trosa 应用", detail: "服务器上的主程序", symbol: "checkmark.circle", value: appHealthLabel),
            statusMetric(title: "安全连接", detail: "网站与服务器之间", symbol: "lock.shield", value: tunnelHealthLabel),
            statusMetric(title: "当前版本", detail: "最近一次网站更新", symbol: "arrow.triangle.2.circlepath", value: releaseLabel)
        ])
        healthItems.orientation = .horizontal
        healthItems.alignment = .top
        healthItems.distribution = .fillEqually
        healthItems.spacing = 10

        let statusPanel = makePanel(background: TrosaPalette.raised, cornerRadius: 16)
        let statusStack = NSStackView(views: [statusRow, healthItems])
        statusStack.orientation = .vertical
        statusStack.alignment = .leading
        statusStack.spacing = 18
        install(statusStack, in: statusPanel, inset: 18)

        let shortcuts = NSStackView(views: [
            quickAction(symbol: "folder.badge.plus", title: "上传客户资料", detail: "Excel、附件和客户文件", buttonTitle: "去上传", action: #selector(showFiles)),
            quickAction(symbol: "folder", title: "管理服务器文件", detail: "查看、下载、整理和回收", buttonTitle: "打开文件", action: #selector(showFiles)),
            quickAction(symbol: "arrow.up.doc", title: "更新网站", detail: "把本机改动同步并上线", buttonTitle: "更新网站", action: #selector(showDeploy)),
            quickAction(symbol: "externaldrive.badge.checkmark", title: "立即备份", detail: "保留一份到这台 Mac", buttonTitle: "立即备份", action: #selector(createBackup))
        ])
        shortcuts.orientation = .horizontal
        shortcuts.alignment = .top
        shortcuts.distribution = .fillEqually
        shortcuts.spacing = 10

        let maintenanceTitle = sectionCaption("遇到异常时再用", "下面的操作会影响服务器运行；日常上传文件、更新网站和备份不需要用到它们。")
        let maintenanceActions = NSStackView(views: [
            button("重启 trosa 应用", #selector(restartTradeOS)),
            button("重启安全连接", #selector(restartTunnel)),
            button("查看运行记录", #selector(showLogs)),
            button("打开服务器终端", #selector(openServerTerminal)),
            button("更多服务器操作", #selector(showAdvancedMaintenance))
        ])
        maintenanceActions.spacing = 8

        let stack = NSStackView(views: [heading, statusPanel, sectionCaption("日常操作", "从这里进入最常用的四件事。"), shortcuts, maintenanceTitle, maintenanceActions])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        add(stack, to: view)
        return view
    }

    private func makeFilesTab() -> NSView {
        let view = tabContainer()
        let heading = pageHeading(
            kicker: "文件与资料",
            title: "把服务器当成一个清楚的文件柜",
            description: "可直接上传客户资料、Excel 和附件，也可以浏览任意服务器文件。删除的内容会先进入回收站，7 天后才会清理。"
        )
        let permissionNote = NSTextField(wrappingLabelWithString: "首次使用时，macOS 可能会询问是否允许访问“桌面”文件夹。请选择“允许”即可；这是读取本机 trosa 项目和你主动选择上传文件所必需的一次性系统权限。")
        permissionNote.font = NSFont.systemFont(ofSize: 11)
        permissionNote.textColor = TrosaPalette.softInk
        permissionNote.maximumNumberOfLines = 2

        let places = NSStackView(views: [
            button("客户资料", #selector(openImportsDirectory), compact: true),
            button("应用数据", #selector(openDataDirectory), compact: true),
            button("回收站", #selector(openTrashDirectory), compact: true),
            button("服务器根目录", #selector(openRootDirectory), compact: true),
            NSView(),
            button("上一级", #selector(goToParentFolder), compact: true)
        ])
        places.orientation = .horizontal
        places.alignment = .centerY
        places.spacing = 8

        pathField.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        pathField.placeholderString = "/var/lib/trade-os"
        pathField.backgroundColor = TrosaPalette.raised
        let pathRow = NSStackView(views: [NSTextField(labelWithString: "当前文件夹"), pathField, button("打开", #selector(refreshFiles), compact: true)])
        pathRow.spacing = 8
        pathField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        fileStatusLabel.font = NSFont.systemFont(ofSize: 12)
        fileStatusLabel.textColor = TrosaPalette.softInk

        let name = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("name"))
        name.title = "名称"
        name.width = 360
        let kind = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("kind"))
        kind.title = "类型"
        kind.width = 90
        let size = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("size"))
        size.title = "大小"
        size.width = 110
        let modified = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("modified"))
        modified.title = "修改时间"
        modified.width = 170
        tableView.addTableColumn(name)
        tableView.addTableColumn(kind)
        tableView.addTableColumn(size)
        tableView.addTableColumn(modified)
        tableView.headerView = NSTableHeaderView()
        tableView.delegate = self
        tableView.dataSource = self
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.doubleAction = #selector(openSelectedEntry)
        tableView.target = self
        let tableScroll = NSScrollView()
        tableScroll.hasVerticalScroller = true
        tableScroll.borderType = .bezelBorder
        tableScroll.documentView = tableView
        tableScroll.setContentHuggingPriority(.defaultLow, for: .vertical)

        let actions = NSStackView(views: [
            primaryButton("上传客户资料 / 文件", #selector(uploadFiles)),
            button("下载选中项目", #selector(downloadSelected)),
            button("新建文件夹", #selector(createDirectory)),
            button("移入回收站", #selector(moveSelectedToTrash))
        ])
        actions.spacing = 8
        let note = NSTextField(wrappingLabelWithString: "提示：双击文件夹可以进入；下载文件或移入回收站前，请先在列表中选中它。")
        note.textColor = TrosaPalette.mutedInk
        note.font = NSFont.systemFont(ofSize: 11)
        let stack = NSStackView(views: [heading, permissionNote, places, pathRow, fileStatusLabel, tableScroll, actions, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        add(stack, to: view)
        NSLayoutConstraint.activate([
            tableScroll.leadingAnchor.constraint(equalTo: stack.leadingAnchor),
            tableScroll.trailingAnchor.constraint(equalTo: stack.trailingAnchor),
            tableScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 260)
        ])
        return view
    }

    private func makeDeployTab() -> NSView {
        let view = tabContainer()
        let heading = pageHeading(
            kicker: "更新网站",
            title: "改完本机项目后，在这里上线",
            description: "“保存并同步上线”会先把你的本机修改保存到 GitHub，再更新服务器。若 GitHub 没有同步成功，网站不会被改动。"
        )

        gitStatusLabel.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        gitStatusLabel.textColor = TrosaPalette.ink
        releaseLabel.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        let gitPanel = statusMetric(title: "本机项目", detail: "是否有尚未保存的修改", symbol: "doc.badge.gearshape", value: gitStatusLabel)
        let releasePanel = statusMetric(title: "网站版本", detail: "当前在服务器上运行的版本", symbol: "globe", value: deployReleaseLabel)
        let stateRow = NSStackView(views: [gitPanel, releasePanel])
        stateRow.orientation = .horizontal
        stateRow.alignment = .top
        stateRow.distribution = .fillEqually
        stateRow.spacing = 10

        let mainAction = primaryButton("保存并同步上线", #selector(saveAndPublish))
        let checkAction = button("检查本机修改", #selector(checkGit))
        let actionRow = NSStackView(views: [mainAction, checkAction])
        actionRow.spacing = 8
        let note = NSTextField(wrappingLabelWithString: "这不会上传客户数据、附件、密码或本机备份。客户资料请在“文件与资料”里操作；代码更新才在这里进行。")
        note.textColor = TrosaPalette.softInk
        note.font = NSFont.systemFont(ofSize: 12)

        let advancedTitle = sectionCaption("其他操作", "只有在你明确知道用途时才需要使用。每次更新都保留可回退的服务器版本。")
        let advancedActions = NSStackView(views: [
            button("仅更新网站（不推荐）", #selector(publishCurrent)),
            button("同步 GitHub", #selector(pushGitHub)),
            button("回退到上一版本", #selector(rollback)),
            button("打开 GitHub", #selector(openGitHub))
        ])
        advancedActions.spacing = 8
        let stack = NSStackView(views: [heading, stateRow, actionRow, note, advancedTitle, advancedActions])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        add(stack, to: view)
        return view
    }

    private func makeBackupTab() -> NSView {
        let view = tabContainer()
        let heading = pageHeading(
            kicker: "数据备份",
            title: "每天自动留一份可以恢复的数据",
            description: "备份会从正在运行的服务器创建一致性快照并下载到这台 Mac；每份都核对校验值，自动保留最近 14 天。"
        )

        backupScheduleLabel.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        backupScheduleLabel.textColor = TrosaPalette.ink
        backupLabel.font = NSFont.systemFont(ofSize: 13)
        backupLabel.textColor = TrosaPalette.softInk
        let backupPanel = makePanel(background: TrosaPalette.mossSoft, cornerRadius: 16)
        let scheduleTitle = NSTextField(labelWithString: "自动备份")
        scheduleTitle.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        scheduleTitle.textColor = TrosaPalette.moss
        let backupStack = NSStackView(views: [scheduleTitle, backupScheduleLabel, backupLabel])
        backupStack.orientation = .vertical
        backupStack.alignment = .leading
        backupStack.spacing = 5
        install(backupStack, in: backupPanel, inset: 18)

        let actions = NSStackView(views: [
            primaryButton("立即创建一份备份", #selector(createBackup)),
            button("在 Finder 中查看备份", #selector(openBackupDirectory)),
            button("刷新备份状态", #selector(refreshLocalBackups))
        ])
        actions.spacing = 8
        backupListLabel.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        backupListLabel.textColor = TrosaPalette.softInk
        backupListLabel.maximumNumberOfLines = 5
        let recentTitle = sectionCaption("最近的备份", "需要恢复时，请先联系我或在技术记录中查看完整校验信息。")
        let stack = NSStackView(views: [heading, backupPanel, actions, recentTitle, backupListLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        add(stack, to: view)
        return view
    }

    private func makePanel(background: NSColor, cornerRadius: CGFloat) -> NSView {
        let panel = NSView()
        panel.translatesAutoresizingMaskIntoConstraints = false
        panel.wantsLayer = true
        panel.layer?.backgroundColor = background.cgColor
        panel.layer?.cornerRadius = cornerRadius
        panel.layer?.borderColor = TrosaPalette.line.cgColor
        panel.layer?.borderWidth = 1
        return panel
    }

    private func install(_ content: NSView, in container: NSView, inset: CGFloat) {
        content.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: inset),
            content.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -inset),
            content.topAnchor.constraint(equalTo: container.topAnchor, constant: inset),
            content.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -inset)
        ])
    }

    private func pageHeading(kicker: String, title: String, description: String) -> NSStackView {
        let eyebrow = NSTextField(labelWithString: kicker)
        eyebrow.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        eyebrow.textColor = TrosaPalette.clay
        let headline = NSTextField(labelWithString: title)
        headline.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        headline.textColor = TrosaPalette.ink
        let detail = NSTextField(wrappingLabelWithString: description)
        detail.font = NSFont.systemFont(ofSize: 13)
        detail.textColor = TrosaPalette.softInk
        detail.maximumNumberOfLines = 2
        let stack = NSStackView(views: [eyebrow, headline, detail])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        return stack
    }

    private func sectionCaption(_ title: String, _ description: String) -> NSStackView {
        let label = NSTextField(labelWithString: title)
        label.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        label.textColor = TrosaPalette.ink
        let detail = NSTextField(wrappingLabelWithString: description)
        detail.font = NSFont.systemFont(ofSize: 12)
        detail.textColor = TrosaPalette.softInk
        detail.maximumNumberOfLines = 2
        let stack = NSStackView(views: [label, detail])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        return stack
    }

    private func statusMetric(title: String, detail: String, symbol: String, value: NSTextField) -> NSView {
        let panel = makePanel(background: TrosaPalette.paper, cornerRadius: 12)
        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        icon.contentTintColor = TrosaPalette.mist
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.setContentHuggingPriority(.required, for: .horizontal)
        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        titleLabel.textColor = TrosaPalette.ink
        let detailLabel = NSTextField(wrappingLabelWithString: detail)
        detailLabel.font = NSFont.systemFont(ofSize: 10)
        detailLabel.textColor = TrosaPalette.mutedInk
        detailLabel.maximumNumberOfLines = 2
        value.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        value.textColor = TrosaPalette.ochre
        value.lineBreakMode = .byTruncatingMiddle
        let text = NSStackView(views: [titleLabel, value, detailLabel])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 3
        let row = NSStackView(views: [icon, text])
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = 9
        install(row, in: panel, inset: 12)
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 18),
            icon.heightAnchor.constraint(equalToConstant: 18),
            panel.heightAnchor.constraint(greaterThanOrEqualToConstant: 88)
        ])
        return panel
    }

    private func quickAction(symbol: String, title: String, detail: String, buttonTitle: String, action: Selector) -> NSView {
        let panel = makePanel(background: TrosaPalette.paper, cornerRadius: 12)
        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        icon.contentTintColor = TrosaPalette.clay
        icon.translatesAutoresizingMaskIntoConstraints = false
        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        titleLabel.textColor = TrosaPalette.ink
        let detailLabel = NSTextField(wrappingLabelWithString: detail)
        detailLabel.font = NSFont.systemFont(ofSize: 11)
        detailLabel.textColor = TrosaPalette.softInk
        detailLabel.maximumNumberOfLines = 2
        let text = NSStackView(views: [titleLabel, detailLabel])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 3
        let actionButton = button(buttonTitle, action, compact: true)
        let row = NSStackView(views: [icon, text, NSView(), actionButton])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        install(row, in: panel, inset: 12)
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 18),
            icon.heightAnchor.constraint(equalToConstant: 18),
            panel.heightAnchor.constraint(greaterThanOrEqualToConstant: 84)
        ])
        return panel
    }

    private func tabContainer() -> NSView {
        let view = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
        return view
    }

    private func tabItem(_ label: String, _ content: NSView) -> NSTabViewItem {
        let item = NSTabViewItem(identifier: label)
        item.label = label
        item.view = content
        return item
    }

    private func add(_ stack: NSStackView, to view: NSView) {
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -14),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -14)
        ])
    }

    private func button(_ title: String, _ action: Selector, compact: Bool = false) -> NSButton {
        let item = NSButton(title: title, target: self, action: action)
        item.bezelStyle = .rounded
        item.font = NSFont.systemFont(ofSize: compact ? 12 : 13, weight: .medium)
        item.controlSize = compact ? .small : .regular
        item.contentTintColor = TrosaPalette.softInk
        return item
    }

    private func primaryButton(_ title: String, _ action: Selector) -> NSButton {
        let item = button(title, action)
        item.bezelStyle = .rounded
        item.contentTintColor = TrosaPalette.clay
        item.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        return item
    }

    private func updateProjectLabel() {
        projectLabel.stringValue = "管理这台 Mac 上的 trosa 项目"
        projectLabel.toolTip = projectRoot.path
    }

    private func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private func workbenchPath() -> String {
        for candidate in ["/usr/local/bin/workbench", "/opt/homebrew/bin/workbench"] {
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return "/usr/local/bin/workbench"
    }

    private func config() -> TrosaConfig? {
        guard let config = TrosaConfig.load(projectRoot: projectRoot) else {
            output("找不到服务器配置。请先选择包含 deploy/cloud/workbench.env 的 trosa 项目目录。")
            setActivity("没有找到 trosa 项目", detail: "请点击右上角“切换项目位置”重新选择。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
            return nil
        }
        return config
    }

    private func output(_ text: String) {
        DispatchQueue.main.async {
            self.outputView.string = text
            self.outputView.scrollToEndOfDocument(nil)
        }
    }

    private func setActivity(_ title: String, detail: String = "", tone: NSColor = TrosaPalette.mist, symbol: String = "circle.dashed") {
        DispatchQueue.main.async {
            self.activityLabel.stringValue = title
            self.activityDetailLabel.stringValue = detail
            self.activityIcon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
            self.activityIcon.contentTintColor = tone
        }
    }

    private func commandSucceeded(_ text: String) -> Bool {
        !text.hasPrefix("命令失败") && !text.hasPrefix("无法执行")
    }

    private func setHealthLabel(_ label: NSTextField, text: String, healthy: Bool) {
        label.stringValue = text
        label.textColor = healthy ? TrosaPalette.moss : TrosaPalette.danger
    }

    private func releaseID(from text: String) -> String? {
        if let machineStatus = managerStatus(from: text), let release = machineStatus["release"], !release.isEmpty, release != "none" {
            return release
        }
        for line in text.split(separator: "\n") {
            let value = String(line).trimmingCharacters(in: .whitespacesAndNewlines)
            if value.hasPrefix("published ") {
                return String(value.dropFirst("published ".count)).trimmingCharacters(in: .whitespaces)
            }
            if value.hasPrefix("rolled back to ") {
                return URL(fileURLWithPath: String(value.dropFirst("rolled back to ".count))).lastPathComponent
            }
            if let range = value.range(of: "/releases/") {
                let suffix = value[range.upperBound...]
                let release = suffix.split(separator: "/").first.map(String.init) ?? ""
                if !release.isEmpty { return release }
            }
        }
        return nil
    }

    private func managerStatus(from text: String) -> [String: String]? {
        guard let line = text.split(separator: "\n").first(where: { $0.hasPrefix("TROSA_MANAGER_STATUS ") }) else { return nil }
        var values: [String: String] = [:]
        for field in line.split(separator: " ").dropFirst() {
            let parts = field.split(separator: "=", maxSplits: 1).map(String.init)
            if parts.count == 2 { values[parts[0]] = parts[1] }
        }
        return values
    }

    @objc private func toggleTechnicalLog() {
        setTechnicalLogVisible(outputScroll.isHidden)
    }

    private func setTechnicalLogVisible(_ visible: Bool) {
        DispatchQueue.main.async {
            self.outputScroll.isHidden = !visible
            self.technicalLogHeight?.constant = visible ? 170 : 0
            self.technicalLogToggle.title = visible ? "收起技术记录" : "显示技术记录"
            self.view.window?.layoutIfNeeded()
        }
    }

    @objc private func showFiles() {
        tabs.selectTabViewItem(at: 1)
    }

    @objc private func showDeploy() {
        tabs.selectTabViewItem(at: 2)
    }

    @objc private func showAdvancedMaintenance() {
        let alert = NSAlert()
        alert.messageText = "更多服务器操作"
        alert.informativeText = "只有在需要时才使用：重启整台服务器会让网站短暂不可用；系统更新可能需要几分钟。"
        alert.addButton(withTitle: "重启整台服务器")
        alert.addButton(withTitle: "安装系统更新")
        alert.addButton(withTitle: "取消")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            rebootServer()
        case .alertSecondButtonReturn:
            updateSystem()
        default:
            break
        }
    }

    private func runLocal(_ executable: String, _ arguments: [String], completion: ((String) -> Void)? = nil) {
        CommandRunner.shared.run(executable: executable, arguments: arguments, directory: projectRoot) { text in
            completion?(text)
            if completion == nil { self.output(text) }
        }
    }

    private func runShell(_ command: String, completion: ((String) -> Void)? = nil) {
        runLocal("/bin/zsh", ["-lc", command], completion: completion)
    }

    private func runWorkbench(_ command: String, completion: ((String) -> Void)? = nil) {
        guard let config = config() else { return }
        let arguments = [
            "exec", "--instance-id", config.instanceID, "--region", config.region,
            "--user-name", "root", "--command", command
        ]
        runLocal(workbenchPath(), arguments, completion: completion)
    }

    private func runScript(_ relativePath: String, completion: ((String) -> Void)? = nil) {
        let script = projectRoot.appendingPathComponent(relativePath).path
        runShell("\(shellQuote(script))", completion: completion)
    }

    @objc private func chooseProjectDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "选择 trosa 项目目录"
        if panel.runModal() == .OK, let url = panel.url {
            projectRoot = url
            UserDefaults.standard.set(url.path, forKey: "trosa.projectRoot")
            updateProjectLabel()
            setActivity("已切换 trosa 项目", detail: "正在重新读取服务器与文件状态。", tone: TrosaPalette.mist, symbol: "folder")
            refreshOverview()
            refreshFiles()
            refreshGitStatus(showOutput: false)
        }
    }

    @objc private func openWebsite() {
        let url = config()?.publicURL ?? "https://app.trosa.space"
        if let target = URL(string: url) {
            NSWorkspace.shared.open(target)
            setActivity("已在浏览器打开 trosa", detail: "这是客户日常使用的网站。", tone: TrosaPalette.moss, symbol: "globe")
        }
    }

    @objc private func refreshOverview() {
        statusLabel.stringValue = "正在检查服务器…"
        statusLabel.textColor = TrosaPalette.ink
        statusDetailLabel.stringValue = "正在确认网站、应用和安全连接。"
        checkedAtLabel.stringValue = ""
        setActivity("正在检查服务器", detail: "通常只需要几秒钟。", tone: TrosaPalette.mist, symbol: "arrow.triangle.2.circlepath")
        runScript("deploy/cloud/status-workbench.sh") { [weak self] text in
            DispatchQueue.main.async {
                guard let self else { return }
                self.output(text)
                let managerStatus = self.managerStatus(from: text)
                let publicHealthOK = managerStatus?["health"] == "ok" || text.contains("\"status\":\"ok\"") || text.contains("\"status\": \"ok\"")
                let activeCount = text.components(separatedBy: "active (running)").count - 1
                let appRunning = managerStatus?["app"] == "active" || activeCount >= 1
                let tunnelRunning = managerStatus?["tunnel"] == "active" || activeCount >= 2
                let healthy = self.commandSucceeded(text) && publicHealthOK && appRunning && tunnelRunning
                self.setHealthLabel(self.websiteHealthLabel, text: publicHealthOK ? "可以访问" : "需要检查", healthy: publicHealthOK)
                self.setHealthLabel(self.appHealthLabel, text: appRunning ? "正在运行" : "需要检查", healthy: appRunning)
                self.setHealthLabel(self.tunnelHealthLabel, text: tunnelRunning ? "已连接" : "需要检查", healthy: tunnelRunning)
                let release = self.releaseID(from: text)
                self.releaseLabel.stringValue = release ?? "未读取到"
                self.deployReleaseLabel.stringValue = release ?? "未读取到"
                self.releaseLabel.textColor = release == nil ? TrosaPalette.ochre : TrosaPalette.moss
                self.deployReleaseLabel.textColor = release == nil ? TrosaPalette.ochre : TrosaPalette.moss
                let formatter = DateFormatter()
                formatter.locale = Locale(identifier: "zh_CN")
                formatter.dateFormat = "M 月 d 日 HH:mm"
                self.checkedAtLabel.stringValue = "上次检查：\(formatter.string(from: Date()))"
                if healthy {
                    self.statusLabel.stringValue = "服务器运行正常"
                    self.statusLabel.textColor = TrosaPalette.moss
                    self.statusDetailLabel.stringValue = "网站、trosa 应用和安全连接都可以正常使用。"
                    self.setActivity("一切正常", detail: "网站可以使用，今天无需处理服务器。", tone: TrosaPalette.moss, symbol: "checkmark.circle.fill")
                } else {
                    self.statusLabel.stringValue = "有一项需要检查"
                    self.statusLabel.textColor = TrosaPalette.danger
                    self.statusDetailLabel.stringValue = "请看下方哪一项显示“需要检查”；必要时打开运行记录。"
                    self.setActivity("服务器需要检查", detail: "已保留技术记录；可先尝试重启 trosa 应用。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle.fill")
                }
            }
        }
    }

    @objc private func showLogs() {
        setActivity("正在读取运行记录", detail: "这些内容主要用于排查异常。", tone: TrosaPalette.mist, symbol: "doc.text.magnifyingglass")
        setTechnicalLogVisible(true)
        runScript("deploy/cloud/logs-workbench.sh") { [weak self] text in
            guard let self else { return }
            self.output(text)
            self.setActivity(
                self.commandSucceeded(text) ? "已读取运行记录" : "读取运行记录失败",
                detail: self.commandSucceeded(text) ? "技术记录已在窗口底部展开。" : "请检查网络或服务器连接。",
                tone: self.commandSucceeded(text) ? TrosaPalette.moss : TrosaPalette.danger,
                symbol: self.commandSucceeded(text) ? "checkmark.circle" : "exclamationmark.triangle"
            )
        }
    }

    @objc private func restartTradeOS() {
        setActivity("正在重启 trosa 应用", detail: "网站可能有几秒钟暂时不可用。", tone: TrosaPalette.ochre, symbol: "arrow.triangle.2.circlepath")
        runWorkbench("systemctl restart trade-os && curl --fail --silent --show-error http://127.0.0.1:8080/api/network/ping") { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "trosa 应用已重启" : "trosa 应用重启失败", detail: succeeded ? "正在重新检查服务器状态。" : "请打开运行记录查看原因。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
            self.refreshOverview()
        }
    }

    @objc private func restartTunnel() {
        setActivity("正在重启安全连接", detail: "网站连接会在几秒内恢复。", tone: TrosaPalette.ochre, symbol: "lock.shield")
        runWorkbench("systemctl restart cloudflared && systemctl is-active cloudflared") { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "安全连接已重启" : "安全连接重启失败", detail: succeeded ? "正在重新检查网站访问。" : "请打开运行记录查看原因。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
            self.refreshOverview()
        }
    }

    @objc private func rebootServer() {
        setActivity("已请求重启整台服务器", detail: "服务器将在约 1 分钟后重启，网站会短暂不可用。", tone: TrosaPalette.ochre, symbol: "power")
        runWorkbench("shutdown -r +1 'trosa manager requested reboot'") { [weak self] text in
            self?.output(text)
        }
    }

    @objc private func updateSystem() {
        setActivity("正在安装服务器系统更新", detail: "这可能需要几分钟，完成前请不要关闭工作台。", tone: TrosaPalette.ochre, symbol: "arrow.down.circle")
        runWorkbench("export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get -y upgrade") { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "系统更新已完成" : "系统更新没有完成", detail: succeeded ? "建议再检查一次服务器状态。" : "请查看技术记录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
        }
    }

    @objc private func openServerTerminal() {
        guard let config = config() else { return }
        let command = "workbench connect --instance-id \(shellQuote(config.instanceID)) --region \(shellQuote(config.region))"
        let escaped = command.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "tell application \"Terminal\" to do script \"\(escaped)\""
        setActivity("正在打开服务器终端", detail: "终端适合需要手动排查时使用。", tone: TrosaPalette.mist, symbol: "terminal")
        runLocal("/usr/bin/osascript", ["-e", script]) { [weak self] text in
            self?.output(text)
            self?.setActivity("已打开服务器终端", detail: "如果不熟悉命令，优先使用本工作台的日常操作。", tone: TrosaPalette.moss, symbol: "checkmark.circle")
        }
    }

    @objc private func refreshFiles() {
        currentRemotePath = pathField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard currentRemotePath.hasPrefix("/") else {
            output("远程路径必须是绝对路径，例如 /var/lib/trade-os")
            fileStatusLabel.stringValue = "路径需要以 / 开头，例如 /var/lib/trade-os"
            fileStatusLabel.textColor = TrosaPalette.danger
            setActivity("文件夹路径不正确", detail: "请输入以 / 开头的服务器路径。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
            return
        }
        fileStatusLabel.stringValue = "正在读取 \(currentRemotePath)"
        fileStatusLabel.textColor = TrosaPalette.softInk
        let quotedPath = shellQuote(currentRemotePath)
        let command = """
        set -eu
        ROOT=\(quotedPath)
        export ROOT
        python3 - <<'PY'
        import json, os
        root = os.environ['ROOT']
        for entry in sorted(os.scandir(root), key=lambda item: item.name.lower()):
            stat = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                kind = 'directory'
            elif entry.is_symlink():
                kind = 'symlink'
            else:
                kind = 'file'
            print(json.dumps({'name': entry.name, 'kind': kind, 'size': stat.st_size, 'modified': int(stat.st_mtime)}, ensure_ascii=False))
        PY
        """
        runWorkbench(command) { [weak self] text in
            guard let self else { return }
            let parsed = text.split(separator: "\n").compactMap { line -> RemoteEntry? in
                guard let data = String(line).data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let name = object["name"] as? String,
                      let kind = object["kind"] as? String else { return nil }
                let size = (object["size"] as? NSNumber)?.int64Value ?? 0
                let modified = (object["modified"] as? NSNumber)?.doubleValue ?? 0
                let date = Date(timeIntervalSince1970: modified)
                let formatter = DateFormatter()
                formatter.dateFormat = "yyyy-MM-dd HH:mm"
                return RemoteEntry(name: name, kind: kind, size: size, modified: formatter.string(from: date))
            }
            DispatchQueue.main.async {
                guard self.commandSucceeded(text) else {
                    self.fileStatusLabel.stringValue = "无法读取这个文件夹；请检查路径或服务器状态。"
                    self.fileStatusLabel.textColor = TrosaPalette.danger
                    self.output(text)
                    self.setActivity("无法读取服务器文件", detail: "请检查路径，或先回到“概览”检查服务器。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                    return
                }
                self.entries = parsed.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
                self.tableView.reloadData()
                self.fileStatusLabel.stringValue = "当前文件夹共有 \(parsed.count) 个项目；双击文件夹可以进入。"
                self.fileStatusLabel.textColor = TrosaPalette.moss
            }
        }
    }

    @objc private func openImportsDirectory() {
        openRemoteDirectory("/var/lib/trade-os/imports")
    }

    @objc private func openDataDirectory() {
        openRemoteDirectory("/var/lib/trade-os")
    }

    @objc private func openTrashDirectory() {
        openRemoteDirectory("/var/lib/trade-os/.trosa-trash")
    }

    @objc private func openRootDirectory() {
        openRemoteDirectory("/")
    }

    @objc private func goToParentFolder() {
        guard currentRemotePath != "/" else { return }
        let parent = URL(fileURLWithPath: currentRemotePath).deletingLastPathComponent().path
        openRemoteDirectory(parent.isEmpty ? "/" : parent)
    }

    private func openRemoteDirectory(_ path: String) {
        currentRemotePath = path
        pathField.stringValue = path
        refreshFiles()
    }

    @objc private func openSelectedEntry() {
        let row = tableView.clickedRow >= 0 ? tableView.clickedRow : tableView.selectedRow
        guard row >= 0, row < entries.count, entries[row].isDirectory else { return }
        let name = entries[row].name
        currentRemotePath = currentRemotePath == "/" ? "/\(name)" : "\(currentRemotePath)/\(name)"
        pathField.stringValue = currentRemotePath
        refreshFiles()
    }

    @objc private func uploadFiles() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.prompt = "上传到服务器"
        guard panel.runModal() == .OK else { return }
        guard let config = config() else { return }
        setActivity("正在上传 \(panel.urls.count) 个文件", detail: "文件会放入当前服务器文件夹。", tone: TrosaPalette.mist, symbol: "arrow.up.doc")
        let remoteDirectory = currentRemotePath.hasSuffix("/") ? currentRemotePath : currentRemotePath + "/"
        upload(urls: panel.urls, index: 0, remoteDirectory: remoteDirectory, config: config)
    }

    private func upload(urls: [URL], index: Int, remoteDirectory: String, config: TrosaConfig) {
        guard index < urls.count else {
            output("上传完成：\(urls.count) 个文件")
            setActivity("文件上传完成", detail: "已放入当前服务器文件夹。", tone: TrosaPalette.moss, symbol: "checkmark.circle.fill")
            refreshFiles()
            return
        }
        let url = urls[index]
        let args = ["upload", url.path, remoteDirectory, "--instance-id", config.instanceID, "--region", config.region, "--force"]
        runLocal(workbenchPath(), args) { [weak self] text in
            guard let self else { return }
            self.output("\(url.lastPathComponent)\n\(text)")
            guard self.commandSucceeded(text) else {
                self.setActivity("文件上传没有完成", detail: "\(url.lastPathComponent) 上传失败，请查看技术记录。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                return
            }
            self.setActivity("正在上传文件", detail: "已完成 \(index + 1) / \(urls.count)：\(url.lastPathComponent)", tone: TrosaPalette.mist, symbol: "arrow.up.doc")
            self.upload(urls: urls, index: index + 1, remoteDirectory: remoteDirectory, config: config)
        }
    }

    @objc private func downloadSelected() {
        let row = tableView.selectedRow
        guard row >= 0, row < entries.count else {
            setActivity("请先选择一个文件", detail: "在列表中点选文件后，再点击“下载选中项目”。", tone: TrosaPalette.ochre, symbol: "hand.point.up.left")
            return
        }
        guard !entries[row].isDirectory else {
            setActivity("请先进入这个文件夹", detail: "当前版本支持下载文件；双击文件夹后选择里面的文件。", tone: TrosaPalette.ochre, symbol: "folder")
            return
        }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.prompt = "选择下载目录"
        guard panel.runModal() == .OK, let destination = panel.url, let config = config() else { return }
        let remotePath = currentRemotePath == "/" ? "/\(entries[row].name)" : "\(currentRemotePath)/\(entries[row].name)"
        let args = ["download", remotePath, destination.path, "--instance-id", config.instanceID, "--region", config.region, "--force"]
        setActivity("正在下载 \(entries[row].name)", detail: "会保存到你选择的 Mac 文件夹。", tone: TrosaPalette.mist, symbol: "arrow.down.doc")
        runLocal(workbenchPath(), args) { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "文件已下载到 Mac" : "文件下载失败", detail: succeeded ? "已保存 \(entries[row].name)。" : "请查看技术记录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
        }
    }

    @objc private func createDirectory() {
        let alert = NSAlert()
        alert.messageText = "新建服务器文件夹"
        alert.informativeText = "输入文件夹名称"
        let field = NSTextField(string: "新文件夹")
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "创建")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let name = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !name.contains("/") else {
            setActivity("文件夹名称不正确", detail: "名称不能为空，也不能包含 /。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
            return
        }
        setActivity("正在新建文件夹", detail: "\(name)", tone: TrosaPalette.mist, symbol: "folder.badge.plus")
        runWorkbench("mkdir -p \(shellQuote(joinRemote(currentRemotePath, name)))") { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "已新建文件夹" : "新建文件夹失败", detail: succeeded ? "\(name) 已在当前文件夹中。" : "请查看技术记录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
            if succeeded { self.refreshFiles() }
        }
    }

    @objc private func moveSelectedToTrash() {
        let row = tableView.selectedRow
        guard row >= 0, row < entries.count else {
            setActivity("请先选择要移走的项目", detail: "选择后会移入服务器回收站，并保留 7 天。", tone: TrosaPalette.ochre, symbol: "trash")
            return
        }
        let remotePath = joinRemote(currentRemotePath, entries[row].name)
        let protectedPaths = ["/", "/boot", "/dev", "/etc", "/home", "/opt", "/proc", "/root", "/sys", "/usr", "/var"]
        guard !protectedPaths.contains(remotePath) else {
            setActivity("不能直接移走系统根目录", detail: "请进入目录后操作其中的具体文件。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
            return
        }
        let source = shellQuote(remotePath)
        let command = """
        set -eu
        TRASH='/var/lib/trade-os/.trosa-trash'
        stamp=$(date +%Y%m%d%H%M%S)
        mkdir -p "$TRASH/$stamp"
        mv -- \(source) "$TRASH/$stamp/"
        find "$TRASH" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf -- {} +
        """
        setActivity("正在移入服务器回收站", detail: "\(entries[row].name) 会保留 7 天。", tone: TrosaPalette.ochre, symbol: "trash")
        runWorkbench(command) { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "已移入服务器回收站" : "移入回收站失败", detail: succeeded ? "\(self.entries[row].name) 将在 7 天后自动清理。" : "请查看技术记录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
            if succeeded { self.refreshFiles() }
        }
    }

    @objc private func checkGit() {
        refreshGitStatus(showOutput: true)
    }

    private func refreshGitStatus(showOutput: Bool) {
        runLocal("/usr/bin/git", ["status", "--short"]) { [weak self] text in
            guard let self else { return }
            if showOutput { self.output(text) }
            let succeeded = self.commandSucceeded(text)
            let changes = text == "完成" ? [] : text.split(separator: "\n").filter { !$0.isEmpty }
            DispatchQueue.main.async {
                guard succeeded else {
                    self.gitStatusLabel.stringValue = "无法读取本机项目"
                    self.gitStatusLabel.textColor = TrosaPalette.danger
                    if showOutput {
                        self.setActivity("无法读取本机项目", detail: "请查看技术记录。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                    }
                    return
                }
                if changes.isEmpty {
                    self.gitStatusLabel.stringValue = "没有待保存的修改"
                    self.gitStatusLabel.textColor = TrosaPalette.moss
                    if showOutput {
                        self.setActivity("本机项目已整理好", detail: "没有发现尚未保存的代码修改。", tone: TrosaPalette.moss, symbol: "checkmark.circle.fill")
                    }
                } else {
                    self.gitStatusLabel.stringValue = "发现 \(changes.count) 处待保存修改"
                    self.gitStatusLabel.textColor = TrosaPalette.ochre
                    if showOutput {
                        self.setActivity("发现 \(changes.count) 处本机修改", detail: "确认无误后，点击“保存并同步上线”。", tone: TrosaPalette.ochre, symbol: "doc.badge.gearshape")
                    }
                }
            }
        }
    }

    @objc private func saveAndPublish() {
        setActivity("正在检查本机修改", detail: "确认后会依次保存到 GitHub，再更新网站。", tone: TrosaPalette.mist, symbol: "arrow.triangle.2.circlepath")
        runLocal("/usr/bin/git", ["status", "--short"]) { [weak self] text in
            guard let self else { return }
            guard self.commandSucceeded(text) else {
                self.output(text)
                self.setActivity("无法读取本机项目", detail: "本次不会更新网站。请查看技术记录。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                return
            }
            let changes = text == "完成" ? [] : text.split(separator: "\n").filter { !$0.isEmpty }
            if changes.isEmpty {
                self.syncGitHubThenPublish()
                return
            }
            DispatchQueue.main.async {
                let formatter = DateFormatter()
                formatter.locale = Locale(identifier: "zh_CN")
                formatter.dateFormat = "yyyy-MM-dd HH:mm"
                let defaultMessage = "update: trosa \(formatter.string(from: Date()))"
                guard let message = self.prompt(text: "这次更新的简单说明", defaultValue: defaultMessage) else { return }
                self.commitSyncAndPublish(message: message)
            }
        }
    }

    private func commitSyncAndPublish(message: String) {
        setActivity("正在保存本机修改", detail: "第 1 步：生成一个可追溯的版本记录。", tone: TrosaPalette.mist, symbol: "square.and.pencil")
        runLocal("/usr/bin/git", ["add", "-A"]) { [weak self] addOutput in
            guard let self else { return }
            guard self.commandSucceeded(addOutput) else {
                self.output(addOutput)
                self.setActivity("无法保存本机修改", detail: "本次不会同步或更新网站。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                return
            }
            self.runLocal("/usr/bin/git", ["commit", "-m", message]) { [weak self] commitOutput in
                guard let self else { return }
                self.output(commitOutput)
                guard self.commandSucceeded(commitOutput) else {
                    self.setActivity("版本记录没有保存", detail: "本次不会同步或更新网站；请查看技术记录。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                    self.refreshGitStatus(showOutput: false)
                    return
                }
                self.syncGitHubThenPublish()
            }
        }
    }

    private func syncGitHubThenPublish() {
        setActivity("正在同步到 GitHub", detail: "第 2 步：为本机代码保存一份云端版本。", tone: TrosaPalette.mist, symbol: "arrow.up.circle")
        runLocal("/usr/bin/git", ["push", "origin", "main"]) { [weak self] text in
            guard let self else { return }
            self.output(text)
            guard self.commandSucceeded(text) else {
                DispatchQueue.main.async {
                    self.gitStatusLabel.stringValue = "GitHub 同步失败，网站未改动"
                    self.gitStatusLabel.textColor = TrosaPalette.danger
                }
                self.setActivity("GitHub 同步没有完成", detail: "为保证版本一致，网站没有更新。请检查网络或 GitHub 登录。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle.fill")
                return
            }
            DispatchQueue.main.async {
                self.gitStatusLabel.stringValue = "已同步到 GitHub"
                self.gitStatusLabel.textColor = TrosaPalette.moss
            }
            self.publishWorktree(reason: "第 3 步：正在把已同步的版本更新到网站。")
        }
    }

    @objc private func pushGitHub() {
        setActivity("正在同步到 GitHub", detail: "本次只同步代码，不会更新网站。", tone: TrosaPalette.mist, symbol: "arrow.up.circle")
        runLocal("/usr/bin/git", ["push", "origin", "main"]) { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            DispatchQueue.main.async {
                self.gitStatusLabel.stringValue = succeeded ? "已同步到 GitHub" : "GitHub 同步失败"
                self.gitStatusLabel.textColor = succeeded ? TrosaPalette.moss : TrosaPalette.danger
            }
            self.setActivity(succeeded ? "GitHub 已同步" : "GitHub 同步失败", detail: succeeded ? "网站还没有更新。" : "请检查网络或 GitHub 登录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
        }
    }

    @objc private func publishCurrent() {
        publishWorktree(reason: "正在直接更新网站；此操作不会先同步 GitHub。")
    }

    private func publishWorktree(reason: String) {
        setActivity("正在更新网站", detail: reason, tone: TrosaPalette.ochre, symbol: "arrow.up.doc")
        runScript("deploy/cloud/publish-workbench.sh") { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            let release = self.releaseID(from: text)
            DispatchQueue.main.async {
                if let release {
                    self.releaseLabel.stringValue = release
                    self.deployReleaseLabel.stringValue = release
                    self.releaseLabel.textColor = TrosaPalette.moss
                    self.deployReleaseLabel.textColor = TrosaPalette.moss
                }
            }
            self.setActivity(succeeded ? "网站已更新" : "网站更新没有完成", detail: succeeded ? "服务器已完成健康检查，可以正常使用。" : "服务器会保留原来的可用版本；请查看技术记录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
            self.refreshGitStatus(showOutput: false)
            self.refreshOverview()
        }
    }

    @objc private func rollback() {
        setActivity("正在回退网站版本", detail: "服务器会切换到上一份可用版本。", tone: TrosaPalette.ochre, symbol: "arrow.uturn.backward.circle")
        runScript("deploy/cloud/rollback-workbench.sh") { [weak self] text in
            guard let self else { return }
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "网站已回退到上一版本" : "网站回退没有完成", detail: succeeded ? "正在重新检查服务器状态。" : "请查看技术记录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
            self.refreshOverview()
        }
    }

    @objc private func openGitHub() {
        runLocal("/usr/bin/git", ["remote", "get-url", "origin"]) { [weak self] text in
            guard let self else { return }
            guard self.commandSucceeded(text) else {
                self.output(text)
                self.setActivity("无法打开 GitHub", detail: "本机项目没有可用的 GitHub 地址。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                return
            }
            var address = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if address.hasPrefix("git@github.com:") {
                address = "https://github.com/" + address.replacingOccurrences(of: "git@github.com:", with: "")
            }
            if address.hasSuffix(".git") { address.removeLast(4) }
            guard let url = URL(string: address) else {
                self.setActivity("无法打开 GitHub", detail: "GitHub 地址格式不正确。", tone: TrosaPalette.danger, symbol: "exclamationmark.triangle")
                return
            }
            DispatchQueue.main.async { NSWorkspace.shared.open(url) }
            self.setActivity("已打开 GitHub", detail: "可在网页中查看完整版本历史。", tone: TrosaPalette.moss, symbol: "checkmark.circle")
        }
    }

    @objc private func createBackup() {
        setActivity("正在创建服务器备份", detail: "会保存到这台 Mac，并自动核对完整性。", tone: TrosaPalette.ochre, symbol: "externaldrive.badge.checkmark")
        runScript("deploy/cloud/backup-workbench.sh") { [weak self] text in
            guard let self else { return }
            self.refreshLocalBackups()
            self.output(text)
            let succeeded = self.commandSucceeded(text)
            self.setActivity(succeeded ? "备份已安全保存到 Mac" : "备份没有完成", detail: succeeded ? "校验值已核对，旧备份会按 14 天规则清理。" : "请查看技术记录。", tone: succeeded ? TrosaPalette.moss : TrosaPalette.danger, symbol: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle")
        }
    }

    @objc private func openBackupDirectory() {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/trosa/backups", isDirectory: true)
        try? FileManager.default.createDirectory(at: path, withIntermediateDirectories: true)
        NSWorkspace.shared.open(path)
    }

    @objc private func refreshLocalBackups() {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/trosa/backups", isDirectory: true)
        let keys: Set<URLResourceKey> = [.fileSizeKey, .contentModificationDateKey, .creationDateKey]
        let files = (try? FileManager.default.contentsOfDirectory(at: path, includingPropertiesForKeys: Array(keys))) ?? []
        let archives = files.filter { $0.lastPathComponent.hasPrefix("trosa-backup-") }.sorted { $0.lastPathComponent > $1.lastPathComponent }
        let agentPath = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.trosa.backup.plist")
        backupScheduleLabel.stringValue = FileManager.default.fileExists(atPath: agentPath.path)
            ? "自动备份已启用：每天 03:30 保存到这台 Mac"
            : "自动备份尚未启用：可先创建一份备份，再联系我设置自动任务"
        guard let latest = archives.first else {
            backupLabel.stringValue = "还没有可用备份。建议现在先创建第一份。"
            backupListLabel.stringValue = "暂无备份文件"
            return
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M 月 d 日 HH:mm"
        let details = (try? latest.resourceValues(forKeys: keys))
        let date = details?.contentModificationDate ?? details?.creationDate ?? Date.distantPast
        let size = Int64(details?.fileSize ?? 0)
        let sizeText = ByteCountFormatter.string(fromByteCount: size, countStyle: .file)
        backupLabel.stringValue = "最近一份：\(formatter.string(from: date)) · \(sizeText)\n本机已保存 \(archives.count) 份；自动保留最近 14 天。"
        backupListLabel.stringValue = archives.prefix(5).map { archive in
            let values = try? archive.resourceValues(forKeys: keys)
            let itemDate = values?.contentModificationDate ?? values?.creationDate ?? Date.distantPast
            let itemSize = ByteCountFormatter.string(fromByteCount: Int64(values?.fileSize ?? 0), countStyle: .file)
            return "\(formatter.string(from: itemDate))   \(itemSize)"
        }.joined(separator: "\n")
    }

    private func prompt(text: String, defaultValue: String) -> String? {
        let alert = NSAlert()
        alert.messageText = text
        let field = NSTextField(string: defaultValue)
        field.frame = NSRect(x: 0, y: 0, width: 320, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "继续")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil
    }

    private func joinRemote(_ directory: String, _ name: String) -> String {
        directory == "/" ? "/\(name)" : "\(directory.replacingOccurrences(of: "/$", with: "", options: .regularExpression))/\(name)"
    }

    // MARK: - NSTableView

    func numberOfRows(in tableView: NSTableView) -> Int { entries.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row < entries.count, let identifier = tableColumn?.identifier.rawValue else { return nil }
        let cell = NSTableCellView()
        let label = NSTextField(labelWithString: "")
        label.translatesAutoresizingMaskIntoConstraints = false
        label.lineBreakMode = .byTruncatingMiddle
        label.font = NSFont.systemFont(ofSize: 12)
        label.textColor = TrosaPalette.ink
        let entry = entries[row]
        switch identifier {
        case "name":
            let icon = NSImageView()
            let symbol = entry.isDirectory ? "folder" : (entry.kind == "symlink" ? "link" : "doc")
            icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: entry.typeLabel)
            icon.contentTintColor = entry.isDirectory ? TrosaPalette.ochre : TrosaPalette.mist
            icon.translatesAutoresizingMaskIntoConstraints = false
            label.stringValue = entry.name
            cell.addSubview(icon)
            cell.addSubview(label)
            NSLayoutConstraint.activate([
                icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 5),
                icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                icon.widthAnchor.constraint(equalToConstant: 15),
                icon.heightAnchor.constraint(equalToConstant: 15),
                label.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 6),
                label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                label.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
            ])
            return cell
        case "kind": label.stringValue = entry.typeLabel
        case "size": label.stringValue = entry.sizeLabel
        case "modified": label.stringValue = entry.modified
        default: break
        }
        cell.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
        ])
        return cell
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        showMainWindow()
    }

    func showMainWindow() {
        if window == nil {
            let controller = TrosaManagerViewController()
            let newWindow = NSWindow(contentViewController: controller)
            newWindow.title = "trosa 工作台"
            newWindow.setContentSize(NSSize(width: 1120, height: 790))
            newWindow.minSize = NSSize(width: 960, height: 700)
            newWindow.isReleasedWhenClosed = false
            newWindow.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            newWindow.center()
            window = newWindow
        }
        window?.makeKeyAndOrderFront(nil)
        window?.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard !flag else { return true }
        window?.makeKeyAndOrderFront(nil)
        window?.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        guard let window, !window.isVisible else { return }
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct TrosaManagerMain {
    private static var delegate: AppDelegate?

    static func main() {
        let application = NSApplication.shared
        application.setActivationPolicy(.regular)
        delegate = AppDelegate()
        application.delegate = delegate
        delegate?.showMainWindow()
        application.run()
    }
}
