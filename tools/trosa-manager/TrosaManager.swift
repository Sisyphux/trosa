import Cocoa
import Foundation

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
    private let statusLabel = NSTextField(labelWithString: "尚未检查")
    private let projectLabel = NSTextField(labelWithString: "")
    private let pathField = NSTextField(string: "/var/lib/trade-os")
    private let tableView = NSTableView()
    private let backupLabel = NSTextField(labelWithString: "")

    override func loadView() {
        let root = UserDefaults.standard.string(forKey: "trosa.projectRoot")
        projectRoot = root.map { URL(fileURLWithPath: $0, isDirectory: true) } ?? defaultProjectRoot

        let mainView = NSView()
        mainView.wantsLayer = true
        mainView.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        view = mainView

        let header = makeHeader()
        let tabs = NSTabView()
        tabs.translatesAutoresizingMaskIntoConstraints = false
        tabs.addTabViewItem(tabItem("概览", makeOverviewTab()))
        tabs.addTabViewItem(tabItem("文件", makeFilesTab()))
        tabs.addTabViewItem(tabItem("代码发布", makeDeployTab()))
        tabs.addTabViewItem(tabItem("备份", makeBackupTab()))

        let outputScroll = NSScrollView()
        outputScroll.translatesAutoresizingMaskIntoConstraints = false
        outputScroll.hasVerticalScroller = true
        outputScroll.borderType = .bezelBorder
        outputView.isEditable = false
        outputView.isSelectable = true
        outputView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        outputView.textColor = NSColor.textColor
        outputView.backgroundColor = NSColor.textBackgroundColor
        outputScroll.documentView = outputView

        mainView.addSubview(header)
        mainView.addSubview(tabs)
        mainView.addSubview(outputScroll)
        NSLayoutConstraint.activate([
            header.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            header.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            header.topAnchor.constraint(equalTo: mainView.topAnchor, constant: 16),
            header.heightAnchor.constraint(equalToConstant: 42),
            tabs.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            tabs.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            tabs.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 12),
            tabs.bottomAnchor.constraint(equalTo: outputScroll.topAnchor, constant: -12),
            outputScroll.leadingAnchor.constraint(equalTo: mainView.leadingAnchor, constant: 18),
            outputScroll.trailingAnchor.constraint(equalTo: mainView.trailingAnchor, constant: -18),
            outputScroll.bottomAnchor.constraint(equalTo: mainView.bottomAnchor, constant: -16),
            outputScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150)
        ])
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        refreshOverview()
        refreshFiles()
        refreshLocalBackups()
    }

    private func makeHeader() -> NSView {
        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        let title = NSTextField(labelWithString: "trosa Server Manager")
        title.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        title.translatesAutoresizingMaskIntoConstraints = false
        projectLabel.font = NSFont.systemFont(ofSize: 11)
        projectLabel.textColor = .secondaryLabelColor
        projectLabel.translatesAutoresizingMaskIntoConstraints = false
        updateProjectLabel()
        let choose = button("选择项目目录", #selector(chooseProjectDirectory))
        let site = button("打开 trosa", #selector(openWebsite))
        let row = NSStackView(views: [title, projectLabel, NSView(), choose, site])
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

    private func makeOverviewTab() -> NSView {
        let view = tabContainer()
        let title = NSTextField(labelWithString: "服务器概览")
        title.font = NSFont.systemFont(ofSize: 17, weight: .medium)
        statusLabel.font = NSFont.systemFont(ofSize: 13)
        statusLabel.textColor = .secondaryLabelColor
        let actions = NSStackView(views: [
            button("刷新状态", #selector(refreshOverview)),
            button("重启 trosa", #selector(restartTradeOS)),
            button("重启 Tunnel", #selector(restartTunnel)),
            button("查看日志", #selector(showLogs))
        ])
        actions.spacing = 8
        let systemActions = NSStackView(views: [
            button("重启 ECS", #selector(rebootServer)),
            button("系统更新", #selector(updateSystem)),
            button("打开服务器终端", #selector(openServerTerminal))
        ])
        systemActions.spacing = 8
        let note = NSTextField(wrappingLabelWithString: "服务器通过 Workbench 连接，不需要开放公网管理端口。文件操作使用 root 权限，删除的项目文件会先移动到服务器回收站并保留 7 天。")
        note.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, statusLabel, actions, systemActions, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        add(stack, to: view)
        return view
    }

    private func makeFilesTab() -> NSView {
        let view = tabContainer()
        pathField.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        pathField.placeholderString = "/var/lib/trade-os"
        let pathRow = NSStackView(views: [NSTextField(labelWithString: "远程路径"), pathField, button("刷新", #selector(refreshFiles))])
        pathRow.spacing = 8
        pathField.setContentHuggingPriority(.defaultLow, for: .horizontal)

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
            button("上传文件", #selector(uploadFiles)),
            button("下载选中", #selector(downloadSelected)),
            button("新建文件夹", #selector(createDirectory)),
            button("移入回收站", #selector(moveSelectedToTrash))
        ])
        actions.spacing = 8
        let stack = NSStackView(views: [pathRow, tableScroll, actions])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        add(stack, to: view)
        NSLayoutConstraint.activate([
            tableScroll.leadingAnchor.constraint(equalTo: stack.leadingAnchor),
            tableScroll.trailingAnchor.constraint(equalTo: stack.trailingAnchor),
            tableScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 280)
        ])
        return view
    }

    private func makeDeployTab() -> NSView {
        let view = tabContainer()
        let title = NSTextField(labelWithString: "代码同步与发布")
        title.font = NSFont.systemFont(ofSize: 17, weight: .medium)
        let explanation = NSTextField(wrappingLabelWithString: "推荐流程：本地修改 → 提交 Git → 同步 GitHub → 发布 ECS。发布脚本会自动建立独立 release，失败时可回滚。")
        explanation.textColor = .secondaryLabelColor
        let row1 = NSStackView(views: [button("检查本地变更", #selector(checkGit)), button("提交并发布", #selector(commitAndPublish)), button("同步 GitHub", #selector(pushGitHub))])
        let row2 = NSStackView(views: [button("发布当前工作区", #selector(publishCurrent)), button("回滚 ECS 版本", #selector(rollback)), button("打开 GitHub", #selector(openGitHub))])
        row1.spacing = 8
        row2.spacing = 8
        let note = NSTextField(wrappingLabelWithString: "数据目录、虚拟环境和密钥已加入忽略规则，不会被发布包或 Git 提交带走。客户资料请在“文件”页或备份功能中管理。")
        note.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, explanation, row1, row2, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        add(stack, to: view)
        return view
    }

    private func makeBackupTab() -> NSView {
        let view = tabContainer()
        let title = NSTextField(labelWithString: "本地备份")
        title.font = NSFont.systemFont(ofSize: 17, weight: .medium)
        backupLabel.textColor = .secondaryLabelColor
        let actions = NSStackView(views: [button("立即备份到 Mac", #selector(createBackup)), button("打开备份目录", #selector(openBackupDirectory)), button("刷新备份列表", #selector(refreshLocalBackups))])
        actions.spacing = 8
        let note = NSTextField(wrappingLabelWithString: "默认保存到 ~/Library/Application Support/trosa/backups/，每个归档包含数据库、附件、manifest 和校验信息；脚本会自动清理超过 14 天的本地归档。")
        note.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, backupLabel, actions, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        add(stack, to: view)
        return view
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

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let item = NSButton(title: title, target: self, action: action)
        item.bezelStyle = .rounded
        return item
    }

    private func updateProjectLabel() {
        projectLabel.stringValue = projectRoot.path
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
            refreshOverview()
            refreshFiles()
        }
    }

    @objc private func openWebsite() {
        let url = config()?.publicURL ?? "https://app.trosa.space"
        if let target = URL(string: url) { NSWorkspace.shared.open(target) }
    }

    @objc private func refreshOverview() {
        statusLabel.stringValue = "正在检查服务器…"
        statusLabel.textColor = .secondaryLabelColor
        runScript("deploy/cloud/status-workbench.sh") { [weak self] text in
            DispatchQueue.main.async {
                self?.output(text)
                let publicHealthOK = text.contains("\"status\":\"ok\"") || text.contains("\"status\": \"ok\"")
                let running = text.contains("active (running)")
                self?.statusLabel.stringValue = running && publicHealthOK ? "服务器运行正常" : "已完成检查，请查看下方日志"
                self?.statusLabel.textColor = running && publicHealthOK ? .systemGreen : .systemOrange
            }
        }
    }

    @objc private func showLogs() {
        runScript("deploy/cloud/logs-workbench.sh")
    }

    @objc private func restartTradeOS() {
        runWorkbench("systemctl restart trade-os && curl --fail --silent --show-error http://127.0.0.1:8080/api/network/ping")
    }

    @objc private func restartTunnel() {
        runWorkbench("systemctl restart cloudflared && systemctl is-active cloudflared")
    }

    @objc private func rebootServer() {
        runWorkbench("shutdown -r +1 'trosa manager requested reboot'")
    }

    @objc private func updateSystem() {
        runWorkbench("export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get -y upgrade")
    }

    @objc private func openServerTerminal() {
        guard let config = config() else { return }
        let command = "workbench connect --instance-id \(shellQuote(config.instanceID)) --region \(shellQuote(config.region))"
        let escaped = command.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "tell application \"Terminal\" to do script \"\(escaped)\""
        runLocal("/usr/bin/osascript", ["-e", script])
    }

    @objc private func refreshFiles() {
        currentRemotePath = pathField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard currentRemotePath.hasPrefix("/") else {
            output("远程路径必须是绝对路径，例如 /var/lib/trade-os")
            return
        }
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
                self?.entries = parsed.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
                self?.tableView.reloadData()
                if parsed.isEmpty && !text.isEmpty && !text.contains("完成") {
                    self?.output(text)
                }
            }
        }
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
        let remoteDirectory = currentRemotePath.hasSuffix("/") ? currentRemotePath : currentRemotePath + "/"
        upload(urls: panel.urls, index: 0, remoteDirectory: remoteDirectory, config: config)
    }

    private func upload(urls: [URL], index: Int, remoteDirectory: String, config: TrosaConfig) {
        guard index < urls.count else {
            output("上传完成")
            refreshFiles()
            return
        }
        let url = urls[index]
        let args = ["upload", url.path, remoteDirectory, "--instance-id", config.instanceID, "--region", config.region, "--force"]
        runLocal(workbenchPath(), args) { [weak self] text in
            self?.output("已上传 \(url.lastPathComponent)\n\(text)")
            self?.upload(urls: urls, index: index + 1, remoteDirectory: remoteDirectory, config: config)
        }
    }

    @objc private func downloadSelected() {
        let row = tableView.selectedRow
        guard row >= 0, row < entries.count else { output("请先选择一个文件"); return }
        guard !entries[row].isDirectory else { output("当前版本请先进入文件夹后下载文件"); return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.prompt = "选择下载目录"
        guard panel.runModal() == .OK, let destination = panel.url, let config = config() else { return }
        let remotePath = currentRemotePath == "/" ? "/\(entries[row].name)" : "\(currentRemotePath)/\(entries[row].name)"
        let args = ["download", remotePath, destination.path, "--instance-id", config.instanceID, "--region", config.region, "--force"]
        runLocal(workbenchPath(), args)
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
        guard !name.isEmpty, !name.contains("/") else { output("文件夹名称无效"); return }
        runWorkbench("mkdir -p \(shellQuote(joinRemote(currentRemotePath, name)))") { [weak self] _ in self?.refreshFiles() }
    }

    @objc private func moveSelectedToTrash() {
        let row = tableView.selectedRow
        guard row >= 0, row < entries.count else { output("请先选择要移入回收站的文件或文件夹"); return }
        let remotePath = joinRemote(currentRemotePath, entries[row].name)
        let protectedPaths = ["/", "/boot", "/dev", "/etc", "/home", "/opt", "/proc", "/root", "/sys", "/usr", "/var"]
        guard !protectedPaths.contains(remotePath) else { output("为避免破坏服务器，不能直接删除系统根目录；请进入目录后操作具体文件。"); return }
        let source = shellQuote(remotePath)
        let command = """
        set -eu
        TRASH='/var/lib/trade-os/.trosa-trash'
        stamp=$(date +%Y%m%d%H%M%S)
        mkdir -p "$TRASH/$stamp"
        mv -- \(source) "$TRASH/$stamp/"
        find "$TRASH" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf -- {} +
        """
        runWorkbench(command) { [weak self] _ in self?.refreshFiles() }
    }

    @objc private func checkGit() {
        runLocal("/usr/bin/git", ["status", "--short"])
    }

    @objc private func commitAndPublish() {
        guard let message = prompt(text: "提交说明", defaultValue: "chore: update trosa") else { return }
        output("正在提交本地 Git…")
        runLocal("/usr/bin/git", ["add", "-A"]) { [weak self] _ in
            self?.runLocal("/usr/bin/git", ["commit", "-m", message]) { [weak self] commitOutput in
                self?.output("Git 提交完成\n\(commitOutput)\n\n开始发布 ECS…")
                self?.runScript("deploy/cloud/publish-workbench.sh")
            }
        }
    }

    @objc private func pushGitHub() {
        runLocal("/usr/bin/git", ["push", "origin", "main"])
    }

    @objc private func publishCurrent() {
        runScript("deploy/cloud/publish-workbench.sh")
    }

    @objc private func rollback() {
        runScript("deploy/cloud/rollback-workbench.sh")
    }

    @objc private func openGitHub() {
        if let url = URL(string: "https://github.com/Sisyphux/trosa") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func createBackup() {
        runScript("deploy/cloud/backup-workbench.sh") { [weak self] text in
            self?.refreshLocalBackups()
            self?.output(text)
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
        let files = (try? FileManager.default.contentsOfDirectory(at: path, includingPropertiesForKeys: [.fileSizeKey, .creationDateKey])) ?? []
        let archives = files.filter { $0.lastPathComponent.hasPrefix("trosa-backup-") }.sorted { $0.lastPathComponent > $1.lastPathComponent }
        let summary = archives.prefix(14).map { $0.lastPathComponent }.joined(separator: "\n")
        backupLabel.stringValue = archives.isEmpty ? "还没有本地备份" : "本地已有 \(archives.count) 个备份\n\(summary)"
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
        let entry = entries[row]
        switch identifier {
        case "name": label.stringValue = entry.isDirectory ? "📁  \(entry.name)" : entry.name
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
    private var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let controller = TrosaManagerViewController()
        window = NSWindow(contentViewController: controller)
        window.title = "trosa Server Manager"
        window.setContentSize(NSSize(width: 1050, height: 720))
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
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
        application.run()
    }
}
