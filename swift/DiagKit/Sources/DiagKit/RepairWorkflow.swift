import Foundation

/// States of a repair, mirrored from the Python `repairlab.workflows.State`.
/// Declaration order is part of the contract (see contracts/workflow.json).
public enum RepairState: String, Codable, CaseIterable, Sendable {
    case intake
    case diagnosed
    case inRepair = "in_repair"
    case partReplaced = "part_replaced"
    case paired
    case calibrated
    case verified
    case released
    case failed
}

public enum WorkflowError: Error, Equatable {
    case illegalTransition(from: RepairState, to: RepairState)
}

public enum RepairRules {
    public static let needsPairing: Set<String> = ["battery", "display", "camera", "biometric"]
    public static let needsCalibration: Set<String> = ["display", "camera", "biometric"]

    public static let transitions: [RepairState: Set<RepairState>] = [
        .intake: [.diagnosed],
        .diagnosed: [.inRepair, .released],
        .inRepair: [.partReplaced],
        .partReplaced: [.paired, .calibrated, .verified],
        .paired: [.calibrated, .verified],
        .calibrated: [.verified],
        .verified: [.released],
        .released: [],
        .failed: [],
    ]

    /// Steps a replaced component needs before it can be verified.
    public static func requiredSteps(for component: String) -> [RepairState] {
        var steps: [RepairState] = []
        if needsPairing.contains(component) { steps.append(.paired) }
        if needsCalibration.contains(component) { steps.append(.calibrated) }
        return steps
    }

    public static func canTransition(from: RepairState, to: RepairState) -> Bool {
        transitions[from]?.contains(to) ?? false
    }
}

/// Tracks one repair and refuses illegal jumps (e.g. releasing a device that was never verified).
public struct RepairSession: Sendable {
    public let component: String
    public private(set) var state: RepairState = .intake
    public private(set) var history: [RepairState] = [.intake]
    public private(set) var errorCode: String?

    public init(component: String) {
        self.component = component
    }

    public mutating func advance(to next: RepairState) throws {
        guard RepairRules.canTransition(from: state, to: next) else {
            throw WorkflowError.illegalTransition(from: state, to: next)
        }
        state = next
        history.append(next)
    }

    public mutating func fail(code: String) {
        state = .failed
        errorCode = code
        history.append(.failed)
    }

    /// True once every post-replacement step this component needs has been recorded.
    public var stepsComplete: Bool {
        let visited = history
        return RepairRules.requiredSteps(for: component).allSatisfy { visited.contains($0) }
    }
}

/// Genuine-part check, identical to `repairlab.device.is_genuine`.
public enum PartAuthenticator {
    public static func checksum(_ body: String) -> Int {
        var total = 0
        for (index, scalar) in body.unicodeScalars.enumerated() {
            total += Int(scalar.value) * (index + 1)
        }
        return total % 10
    }

    public static func isGenuine(_ serial: String, for component: String) -> Bool {
        let parts = serial.split(separator: "-", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 3, parts[0] == "GP", let check = Int(parts[2]) else { return false }
        let prefix = String(component.prefix(3)).uppercased()
        return parts[1].hasPrefix(prefix) && checksum(parts[1]) == check
    }
}

/// Roll-up of a diagnostic sweep for reporting.
public struct DiagnosticSummary: Equatable, Sendable {
    public let total: Int
    public let passed: Int
    public let failuresByReason: [String: [String]]

    public init(results: [TestResult]) {
        total = results.count
        passed = results.filter { $0.status == .pass }.count
        var grouped: [String: [String]] = [:]
        for result in results where result.status == .fail {
            grouped[result.reason ?? "UNKNOWN", default: []].append(result.test)
        }
        failuresByReason = grouped
    }

    public var passRate: Double { total == 0 ? 0 : Double(passed) / Double(total) }
}
