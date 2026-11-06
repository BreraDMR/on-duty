// Дежурный — menu bar face of codex_wake.py. All the logic lives in the python script,
// this thing just shows `status --json` and runs its commands.
// Since 03.11.2026 it's a small window (click the moon, look, click away), like the Stats app.
import SwiftUI
import UserNotifications
import AppKit

// build.sh writes both into Info.plist, so the app runs the codex_wake.py it was built next to
let python = Bundle.main.object(forInfoDictionaryKey: "DutyPython") as? String ?? "/usr/bin/python3"
let script = Bundle.main.object(forInfoDictionaryKey: "DutyScript") as? String
    ?? NSHomeDirectory() + "/.claude/tools/codex-wake/codex_wake.py"

struct Window: Decodable { let usedPercent: Double?; let resetsAt: Double? }
struct Limits: Decodable { let allowed: Bool; let primary: Window?; let secondary: Window?; let free_resets: Int?; let checked_at: Double? }
struct Thread: Decodable, Identifiable {
    let id: String; let name: String?; let mode: String?; let plan: String?
    let state: String; let state_at: Double?; let wakes: Int; let last_msg: String?
    let open_thread: String?; let pending: Int?  // item-by-item duty: the questions chat, items parked for Damir
    let seat: String?                            // item-by-item duty: where the last run went (Codex / Claude 1 / Claude 2)
}
struct ClaudeWindow: Decodable { let usedPercent: Double?; let resetsAt: Double?; let resetPassed: Bool? }
struct ClaudeAccount: Decodable, Identifiable {
    let account: Int; let updated_at: Double; let five_hour: ClaudeWindow?; let week: ClaudeWindow?
    var id: Int { account }
}
struct Mac: Decodable {
    let mode: String; let until: Double?; let on_battery: Bool; let battery: Int?; let battery_low: Bool
    let holding: Bool; let holders: [String]
}
struct Watcher: Decodable, Identifiable { let sid: String; let name: String; let pid: Int; var id: String { sid } }
struct Carousel: Decodable { let seats: [String: Bool]; let watchers: [Watcher] }
struct TokenCount: Decodable { let total: Double; let fresh: Double }
struct TokenWindows: Decodable { let five_hour: TokenCount?; let week: TokenCount? }
struct Tokens: Decodable { let codex: TokenWindows?; let claude: TokenWindows? }
struct Status: Decodable {
    let threads: [Thread]; let limits: Limits?; let daemon: Bool; let claude: [ClaudeAccount]?
    let mac: Mac?; let carousel: Carousel?; let tokens: Tokens?
}

// 14 600 000 -> "14,6 млн", 1 260 000 000 -> "1,26 млрд"
func tok(_ n: Double) -> String {
    let f = NumberFormatter(); f.locale = Locale(identifier: "ru_RU"); f.numberStyle = .decimal
    if n >= 1e9 { f.maximumFractionDigits = 2; return (f.string(from: NSNumber(value: n / 1e9)) ?? "") + " млрд" }
    if n >= 1e6 { f.maximumFractionDigits = n >= 1e8 ? 0 : 1; return (f.string(from: NSNumber(value: n / 1e6)) ?? "") + " млн" }
    f.maximumFractionDigits = 0; return (f.string(from: NSNumber(value: n / 1e3)) ?? "") + " тыс"
}

struct TokenLine: View {
    let w: TokenWindows?
    var body: some View {
        if let w {
            VStack(alignment: .leading, spacing: 1) {
                row("5 ч", w.five_hour)
                row("нед", w.week)
            }
        }
    }
    func row(_ label: String, _ c: TokenCount?) -> some View {
        HStack(spacing: 6) {
            Text(label).frame(width: 34, alignment: .leading)
            Text(c.map { "\(tok($0.total)) токенов · новых \(tok($0.fresh))" } ?? "—")
        }
        .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
    }
}

@discardableResult
func run(_ args: [String]) -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [script] + args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return "" }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return String(data: data, encoding: .utf8) ?? ""
}

func hhmm(_ t: Double?) -> String {
    guard let t else { return "—" }
    let f = DateFormatter()
    f.dateFormat = Calendar.current.isDateInToday(Date(timeIntervalSince1970: t)) ? "HH:mm" : "dd.MM HH:mm"
    return f.string(from: Date(timeIntervalSince1970: t))
}

func ageText(_ t: Double) -> String? {
    let m = Int(Date().timeIntervalSince1970 - t) / 60
    if m < 10 { return nil }
    return m < 60 ? "данные \(m) мин назад" : m < 48 * 60 ? "данные \(m / 60) ч назад" : "данные \(m / 1440) дн назад"
}

// green / yellow / red, same thresholds everywhere
func levelColor(_ p: Double) -> Color { p >= 90 ? .red : p >= 70 ? .yellow : .green }

final class Model: ObservableObject {
    @Published var status: Status?
    @Published var carouselMsg = ""
    @Published var sid = ""            // no @State: the Command Line Tools swiftc has no SwiftUI macro plugin
    @Published var macMode = "normal"
    private var timer: Timer?
    private var lastAsks: Int?

    init() {
        // osascript notifications from the launchd daemon never showed up (02.11.2026), so the app posts its own
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func refresh(limits: Bool = false) {
        DispatchQueue.global().async {
            if limits { run(["limits"]) }
            let out = run(["status", "--json"])
            let s = try? JSONDecoder().decode(Status.self, from: Data(out.utf8))
            DispatchQueue.main.async {
                self.status = s
                let asks = self.asks
                if let before = self.lastAsks, asks > before { self.notifyAsk(asks) }
                if s != nil { self.lastAsks = asks }
            }
        }
    }

    // opening the window: codex numbers older than 5 min get asked again (costs no tokens)
    func opened() {
        let checked = status?.limits?.checked_at ?? 0
        refresh(limits: Date().timeIntervalSince1970 - checked > 300)
    }

    var asks: Int {
        live.reduce(0) { $0 + ($1.pending ?? 0) } + live.filter { $0.state == "idle" }.count
    }

    func notifyAsk(_ n: Int) {
        post("Дежурный: вопрос", "Ждут твоего ответа: \(n). Открой чат «Дежурный: вопросы» в Codex")
    }

    func post(_ title: String, _ body: String) {
        let c = UNMutableNotificationContent()
        c.title = title; c.body = body; c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }

    func cmd(_ args: [String], then: ((String) -> Void)? = nil) {
        DispatchQueue.global().async {
            let out = run(args)
            DispatchQueue.main.async { then?(out) }
            self.refresh()
        }
    }

    var live: [Thread] { status?.threads.filter { !["done", "paused"].contains($0.state) } ?? [] }

    // one icon, always the same: a changing icon changed the status item's width and the window jumped
    // left and right (03.11.2026); questions for Damir show inside the window instead
    let icon = "moon.stars"
    @Published var showHelp = false
}

// ---------- pieces ----------

struct Bar: View {
    let label: String; let percent: Double?; let reset: Double?; var passed = false
    var body: some View {
        let p = passed ? 0 : min(max(percent ?? 0, 0), 100)
        HStack(spacing: 6) {
            Text(label).font(.caption).foregroundStyle(.secondary).frame(width: 34, alignment: .leading)
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.gray.opacity(0.25))
                    Capsule().fill(levelColor(p)).frame(width: max(g.size.width * p / 100, p > 0 ? 3 : 0))
                }
            }.frame(height: 6)
            Text(percent == nil && !passed ? "—" : "\(Int(p)) %").font(.caption.monospacedDigit()).frame(width: 40, alignment: .trailing)
            Text(passed ? "сброшено" : hhmm(reset)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                .frame(width: 74, alignment: .trailing)
        }
    }
}

struct Block<Content: View>: View {
    let title: String; @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            content
        }
    }
}

func stateText(_ t: Thread) -> String {
    switch t.state {
    case "running": return t.seat.map { "работает · \($0)" } ?? "работает"
    case "limited": return t.seat != nil ? "все места в лимите, ждёт сброса" : "ждёт сброса лимита"
    case "idle": return "ждёт тебя"
    case "done": return "план закрыт"
    case "paused": return "на паузе"
    default: return t.state
    }
}

func stateIcon(_ s: String) -> (String, Color) {
    switch s {
    case "running": return ("play.circle.fill", .green)
    case "limited": return ("moon.circle.fill", .yellow)
    case "idle": return ("questionmark.circle.fill", .orange)
    case "done": return ("checkmark.circle.fill", .secondary)
    case "paused": return ("pause.circle.fill", .secondary)
    default: return ("circle", .secondary)
    }
}

// ---------- the window ----------

struct PanelView: View {
    @ObservedObject var m: Model

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if m.showHelp {
                HelpView(m: m)
            } else {
                if m.asks > 0 { asksBanner }
                limits
                Divider()
                mac
                Divider()
                if m.status?.carousel != nil {  // only when the carousel tool is installed
                    carousel
                    Divider()
                }
                duties
                Divider()
                footer
            }
        }
        .padding(14)
        .frame(width: 330, alignment: .topLeading)
        .fixedSize(horizontal: true, vertical: true)
        .onAppear { m.opened(); m.macMode = m.status?.mac?.mode ?? "normal" }
        .onChange(of: m.status?.mac?.mode) { _, new in if let new { m.macMode = new } }
    }

    var asksBanner: some View {
        Button {
            if let q = m.status?.threads.compactMap({ $0.open_thread }).first {
                NSWorkspace.shared.open(URL(string: "codex://threads/\(q)")!)
            }
        } label: {
            HStack {
                Image(systemName: "questionmark.circle.fill")
                Text("Дежурный ждёт ответа: \(m.asks) — открыть чат вопросов").font(.caption)
                Spacer()
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.18)))
        }
        .buttonStyle(.plain).foregroundStyle(.orange)
    }

    var limits: some View {
        Block(title: "Лимиты") {
            VStack(alignment: .leading, spacing: 3) {
                Text("Codex").font(.callout.weight(.medium))
                if let l = m.status?.limits {
                    Bar(label: "5 ч", percent: l.primary?.usedPercent, reset: l.primary?.resetsAt)
                    Bar(label: "нед", percent: l.secondary?.usedPercent, reset: l.secondary?.resetsAt)
                    TokenLine(w: m.status?.tokens?.codex)
                    if let n = l.free_resets, n > 0 {
                        Text("бесплатных сбросов: \(n) — в приложении Codex").font(.caption2).foregroundStyle(.secondary)
                    }
                } else {
                    Text("ещё не читались").font(.caption).foregroundStyle(.secondary)
                }
            }
            ForEach(m.status?.claude ?? []) { a in
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text("Claude \(a.account)").font(.callout.weight(.medium))
                        Spacer()
                        if let age = ageText(a.updated_at) { Text(age).font(.caption2).foregroundStyle(.secondary) }
                    }
                    Bar(label: "5 ч", percent: a.five_hour?.usedPercent, reset: a.five_hour?.resetsAt,
                        passed: a.five_hour?.resetPassed == true)
                    Bar(label: "нед", percent: a.week?.usedPercent, reset: a.week?.resetsAt, passed: a.week?.resetPassed == true)
                }
            }
            if let t = m.status?.tokens?.claude {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Claude, оба аккаунта вместе").font(.caption.weight(.medium))
                    TokenLine(w: t)
                }
            }
        }
    }

    var mac: some View {
        Block(title: "Мак") {
            Picker("", selection: $m.macMode) {
                Text("Обычный").tag("normal")
                Text("Пока дежурство").tag("duty")
                Text("До 8:00").tag("night")
            }
            .pickerStyle(.segmented).labelsHidden()
            .onChange(of: m.macMode) { _, new in
                if new != m.status?.mac?.mode { m.cmd(["mac-mode", new]) }
            }
            if let mc = m.status?.mac {
                Text(macLine(mc)).font(.caption).foregroundStyle(mc.battery_low ? .orange : .secondary)
                Text("не дают уснуть: " + (mc.holders.isEmpty ? "никто" : mc.holders.joined(separator: ", ")))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
            }
        }
    }

    func macLine(_ mc: Mac) -> String {
        if mc.battery_low && mc.mode != "normal" { return "батарея \(mc.battery ?? 0) % — мак отпущен, режим вернётся на зарядке" }
        switch mc.mode {
        case "duty": return mc.holding ? "держу, пока идёт дежурство" : "держу только во время дежурства — сейчас его нет"
        case "night": return "держу до \(hhmm(mc.until)), потом сам вернусь в «Обычный»"
        default: return "мак живёт по своим настройкам сна"
        }
    }

    var carousel: some View {
        Block(title: "Карусель аккаунтов") {
            if let c = m.status?.carousel {
                HStack(spacing: 10) {
                    // each seat is its own switch: off = neither the duty nor the session carousel goes there
                    ForEach([("1", "Claude 1"), ("2", "Claude 2"), ("3", "Codex")], id: \.0) { k, name in
                        let on = c.seats[k] == true
                        Button { m.cmd(["seat", k, on ? "off" : "on"]) } label: {
                            Label(name, systemImage: on ? "circle.fill" : "circle")
                                .font(.caption).foregroundStyle(on ? .green : .secondary)
                        }
                        .buttonStyle(.borderless).help(on ? "Выключить \(name)" : "Включить \(name)")
                    }
                }
                ForEach(c.watchers) { w in
                    HStack {
                        Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.green)
                        Text(w.name.isEmpty ? String(w.sid.prefix(8)) : "\(w.name) · \(w.sid.prefix(8))").font(.caption)
                        Spacer()
                        Button { m.cmd(["carousel-stop", w.sid]) } label: { Image(systemName: "stop.circle") }
                            .buttonStyle(.borderless).help("Остановить карусель для этой сессии")
                    }
                }
            }
            HStack {
                TextField("ID сессии из /status", text: $m.sid).textFieldStyle(.roundedBorder).font(.caption)
                Button("Запустить") {
                    let id = m.sid.trimmingCharacters(in: .whitespacesAndNewlines)
                    m.carouselMsg = "запускаю…"
                    m.cmd(["carousel-start", id]) { out in
                        m.carouselMsg = out.trimmingCharacters(in: .whitespacesAndNewlines)
                        if out.hasPrefix("watching") || out.contains("already guards") { m.sid = "" }
                    }
                }
                .disabled(m.sid.trimmingCharacters(in: .whitespaces).count < 8)
            }
            if !m.carouselMsg.isEmpty {
                Text(m.carouselMsg).font(.caption2).foregroundStyle(.secondary).lineLimit(3)
            }
        }
    }

    var duties: some View {
        Block(title: "Дежурства") {
            if let threads = m.status?.threads, !threads.isEmpty {
                ForEach(threads) { t in
                    HStack(spacing: 6) {
                        let (ic, col) = stateIcon(t.state)
                        Image(systemName: ic).foregroundStyle(col)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(t.name ?? String(t.id.prefix(8))).font(.callout)
                            Text(stateText(t) + ((t.pending ?? 0) > 0 ? " · ❓ \(t.pending!)" : ""))
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                        threadMenu(t)
                    }
                }
            } else {
                Text("никого — в чате Codex: «на дежурство, план — …»").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    func threadMenu(_ t: Thread) -> some View {
        Menu {
            if t.id.hasPrefix("items-") {
                // runs of an item-by-item duty aren't app threads; questions and answers live in one chat
                if let q = t.open_thread {
                    Button("Открыть чат вопросов") { NSWorkspace.shared.open(URL(string: "codex://threads/\(q)")!) }
                }
                Button("Открыть план") { if let p = t.plan { NSWorkspace.shared.open(URL(fileURLWithPath: p)) } }
            } else {
                Button("Открыть в Codex") { NSWorkspace.shared.open(URL(string: "codex://threads/\(t.id)")!) }
                Button("Разбудить сейчас") { m.cmd(["wake", t.id, "--force"]) }.disabled(t.state == "running")
            }
            Divider()
            if t.state == "paused" {
                Button("Продолжить дежурство") { m.cmd(["resume", t.id]) }
            } else {
                Button("Пауза") { m.cmd(["pause", t.id]) }
            }
            Button("Снять с дежурства") { m.cmd(["remove", t.id]) }
        } label: { Image(systemName: "ellipsis.circle") }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
    }

    var footer: some View {
        HStack(spacing: 14) {
            Button { m.refresh(limits: true) } label: { Image(systemName: "arrow.clockwise") }.help("Обновить лимиты")
            Button {
                NSWorkspace.shared.open(URL(fileURLWithPath: NSHomeDirectory() + "/.claude/tools/codex-wake/state/codex-wake.log"))
            } label: { Image(systemName: "doc.text") }.help("Журнал сторожа")
            Button { m.post("Дежурный: проверка", "Уведомления работают") } label: { Image(systemName: "bell") }
                .help("Проверить уведомление")
            Button { m.cmd(["pause", "all"]) } label: { Image(systemName: "pause.circle") }.help("Пауза всем дежурствам")
            Spacer()
            let on = m.status?.daemon != false
            Button { m.cmd([on ? "off" : "on"]) } label: {
                Label(on ? "сторож вкл" : "сторож выкл", systemImage: "power").font(.caption)
            }
            .foregroundStyle(on ? .green : .secondary).help(on ? "Выключить сторожа" : "Включить сторожа")
            Button { m.showHelp = true } label: { Image(systemName: "book") }.help("Инструкция")
            Button { NSApp.terminate(nil) } label: { Image(systemName: "xmark.circle") }.help("Закрыть значок")
        }
        .buttonStyle(.borderless)
    }
}

struct HelpView: View {
    @ObservedObject var m: Model

    func item(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.callout.weight(.semibold))
            Text(text).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Инструкция").font(.headline)
                Spacer()
                Button { m.showHelp = false } label: { Label("Назад", systemImage: "chevron.left") }.buttonStyle(.borderless)
            }
            item("Лимиты", "Полоски «5 ч» и «нед» у Codex и обоих аккаунтов Claude. Зелёный — до 70 %, жёлтый — до 90 %, красный — дальше. Справа — когда сброс. Токены на это не тратятся. «данные N назад» — цифры давно не обновлялись (у аккаунта протух вход — открой в нём сессию).")
            item("Токены", "Под лимитами — сколько токенов ушло с последнего сброса, по журналам на этом маке. «Новых» — без повторного чтения кэша (это история переписки, которую модель перечитывает каждый шаг, её обычно ~98 %). Оба Claude считаются вместе — их журналы лежат в одной папке; окна — по Claude 1. Чат claude.ai сюда не попадает.")
            item("Мак", "Обычный — мак засыпает как всегда, дежурный его не держит. Пока дежурство — не даёт уснуть, пока кто-то на дежурстве. До 8:00 — не даёт уснуть до утра, потом сам вернётся в «Обычный». На батарее ниже 25 % мак отпускается в любом режиме. Экран гаснет как обычно. «Не дают уснуть» — кто сейчас держит мак.")
            item("Карусель аккаунтов", "В нужной сессии Claude набери /status, скопируй ID сессии, вставь в поле и нажми «Запустить». Когда лимит аккаунта кончится, переписка сама переедет на другой Claude или в Codex. Кружки Claude 1 / Claude 2 / Codex — выключатели мест: нажми, чтобы выключить или включить. Выключенное место не берут ни карусель, ни дежурный. Пока карусель работает, мак не спит. «Стоп» у сессии — выключить.")
            item("Дежурства", "Поставить: в чате Codex «на дежурство, план — <файл>». Дежурство по пунктам берёт для каждого пункта первое свободное включённое место: сначала Codex, потом Claude 1, потом Claude 2; кто сейчас работает — видно под названием. Вопросы дежурного приходят в чат «Дежурный: вопросы» в Codex, наверху окна тогда оранжевая плашка — нажми, откроется чат. «…» у дежурства — план, пауза, снять.")
            item("Кнопки внизу", "Обновить лимиты · журнал сторожа · проверить уведомление · пауза всем дежурствам · сторож вкл/выкл (выключенный никого не будит и мак не держит) · эта инструкция · закрыть значок (открыть снова — Cmd+Пробел → «Дежурный»).")
        }
    }
}

@main
struct DezhurnyApp: App {
    @StateObject var m = Model()
    var body: some Scene {
        MenuBarExtra { PanelView(m: m) } label: { Image(systemName: m.icon) }
            .menuBarExtraStyle(.window)
    }
}
