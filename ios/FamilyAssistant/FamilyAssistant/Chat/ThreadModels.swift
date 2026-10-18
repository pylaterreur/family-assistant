import Foundation

/// The caller's Home (`GET /api/v1/chat/home`). With threads disabled there is
/// no Home and the app keeps its conversation list.
struct ChatHomeResponse: Decodable, Equatable {
    let enabled: Bool
    let conversationID: String?

    enum CodingKeys: String, CodingKey {
        case enabled
        case conversationID = "conversation_id"
    }
}

/// Where a thread stands, as its card and switcher row show it. A state this
/// client does not know yet decodes as `.unknown` rather than failing the list.
enum ChatThreadState: Equatable, Decodable {
    case working
    case waiting
    case failed
    case done
    case unknown(String)

    init(rawValue: String) {
        switch rawValue {
        case "working": self = .working
        case "waiting": self = .waiting
        case "failed": self = .failed
        case "done": self = .done
        default: self = .unknown(rawValue)
        }
    }

    init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }
}

/// One thread of a Home: what its card and switcher row show.
struct ChatThreadSummary: Decodable, Equatable, Identifiable {
    let conversationID: String
    let homeConversationID: String
    let title: String
    let profileID: String
    let state: ChatThreadState
    let sourceToolCallID: String?
    let seedRequest: String?
    let preview: String?
    let createdAt: Date
    let updatedAt: Date

    var id: String { conversationID }

    /// The line under the title: the thread's latest message, or what it was
    /// asked to do before it has said anything.
    var previewLine: String? {
        for candidate in [preview, seedRequest] {
            if let candidate, !candidate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return candidate
            }
        }
        return nil
    }

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case homeConversationID = "home_conversation_id"
        case title
        case profileID = "profile_id"
        case state
        case sourceToolCallID = "source_tool_call_id"
        case seedRequest = "seed_request"
        case preview
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

struct ChatHomeThreadsResponse: Decodable, Equatable {
    let homeConversationID: String?
    let threads: [ChatThreadSummary]

    enum CodingKeys: String, CodingKey {
        case homeConversationID = "home_conversation_id"
        case threads
    }
}

/// Whether a conversation is the caller's Home, one of its threads, or an
/// ordinary conversation.
enum ChatConversationKind: Equatable {
    case home
    case thread
    case conversation

    init?(rawValue: String?) {
        switch rawValue {
        case "home": self = .home
        case "thread": self = .thread
        case "conversation": self = .conversation
        default: return nil
        }
    }
}

/// The name of the Home-only tool the assistant starts a thread with. Its call
/// id is the started thread's `source_tool_call_id`.
enum ChatThreadTool {
    static let startThreadName = "start_thread"

    /// The title the assistant gave a thread, read from the call's arguments,
    /// for a card whose thread has not reached the thread list yet.
    static func requestedTitle(from argumentsText: String) -> String? {
        guard let data = argumentsText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let title = object["title"] as? String
        else {
            return nil
        }
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

/// A tool group's calls, with those that started a thread taken out to be shown
/// as the thread's card. A call becomes a card when its thread is known, or when
/// it has not failed (a thread that is starting has not reached the list yet).
/// A refused call stays an ordinary tool row: there is no thread to open.
struct ThreadCardSplit {
    let threadCalls: [ChatToolCall]
    let otherCalls: [ChatToolCall]

    init(
        toolCalls: [ChatToolCall],
        showsCards: Bool,
        hasThread: (_ toolCallID: String) -> Bool = { _ in false }
    ) {
        guard showsCards else {
            threadCalls = []
            otherCalls = toolCalls
            return
        }
        var cards: [ChatToolCall] = []
        var others: [ChatToolCall] = []
        for call in toolCalls {
            if Self.isCard(call, hasThread: hasThread) {
                cards.append(call)
            } else {
                others.append(call)
            }
        }
        threadCalls = cards
        otherCalls = others
    }

    private static func isCard(_ call: ChatToolCall, hasThread: (String) -> Bool) -> Bool {
        guard call.name == ChatThreadTool.startThreadName else {
            return false
        }
        if hasThread(call.id) {
            return true
        }
        guard call.status.isPending || call.status == .succeeded else {
            return false
        }
        return !(call.resultText?.hasPrefix("Error:") ?? false)
    }
}

/// The thread switcher's contents: Home, then threads grouped by what they
/// need, then the ordinary conversations under "Earlier".
struct ThreadSwitcherSections: Equatable {
    struct Group: Equatable, Identifiable {
        let title: String
        let threads: [ChatThreadSummary]

        var id: String { title }
    }

    let groups: [Group]
    let earlier: [ChatConversationSummary]

    static let waitingTitle = "Waiting on you"
    static let workingTitle = "Working"
    static let failedTitle = "Failed"
    static let doneTitle = "Done"

    /// Threads keep the server's order (newest first) within their group. A
    /// thread in a state this client does not know is shown as waiting, the
    /// server's own catch-all, so it is never hidden. Earlier holds only
    /// conversations that are neither Home nor one of its threads.
    init(
        threads: [ChatThreadSummary],
        conversations: [ChatConversationSummary],
        homeConversationID: String?
    ) {
        var waiting: [ChatThreadSummary] = []
        var working: [ChatThreadSummary] = []
        var failed: [ChatThreadSummary] = []
        var done: [ChatThreadSummary] = []
        for thread in threads {
            switch thread.state {
            case .working: working.append(thread)
            case .failed: failed.append(thread)
            case .done: done.append(thread)
            case .waiting, .unknown: waiting.append(thread)
            }
        }
        groups = [
            Group(title: Self.waitingTitle, threads: waiting),
            Group(title: Self.workingTitle, threads: working),
            Group(title: Self.failedTitle, threads: failed),
            Group(title: Self.doneTitle, threads: done),
        ].filter { !$0.threads.isEmpty }
        let excluded = Set(threads.map(\.conversationID)).union([homeConversationID].compactMap { $0 })
        earlier = conversations.filter { conversation in
            !excluded.contains(conversation.conversationID)
                && (conversation.kind == nil || conversation.kind == "conversation")
        }
    }

    /// Threads waiting on the user, which the threads button badges.
    static func waitingCount(in threads: [ChatThreadSummary]) -> Int {
        threads.filter { $0.state == .waiting }.count
    }
}
