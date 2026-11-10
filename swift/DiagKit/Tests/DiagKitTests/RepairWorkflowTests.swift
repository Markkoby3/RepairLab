import XCTest
@testable import DiagKit

final class RepairWorkflowTests: XCTestCase {
    func testDisplayRepairWalksEveryRequiredStep() throws {
        var session = RepairSession(component: "display")
        for step: RepairState in [.diagnosed, .inRepair, .partReplaced, .paired, .calibrated, .verified, .released] {
            try session.advance(to: step)
        }
        XCTAssertEqual(session.state, .released)
        XCTAssertTrue(session.stepsComplete)
    }

    func testSpeakerRepairSkipsPairingAndCalibration() throws {
        var session = RepairSession(component: "speaker")
        for step: RepairState in [.diagnosed, .inRepair, .partReplaced, .verified, .released] {
            try session.advance(to: step)
        }
        XCTAssertTrue(session.stepsComplete)
        XCTAssertEqual(RepairRules.requiredSteps(for: "speaker"), [])
    }

    func testBatteryNeedsPairingButNotCalibration() {
        XCTAssertEqual(RepairRules.requiredSteps(for: "battery"), [.paired])
    }

    func testCannotReleaseWithoutVerification() throws {
        var session = RepairSession(component: "camera")
        try session.advance(to: .diagnosed)
        try session.advance(to: .inRepair)
        try session.advance(to: .partReplaced)
        XCTAssertThrowsError(try session.advance(to: .released)) { error in
            XCTAssertEqual(error as? WorkflowError, .illegalTransition(from: .partReplaced, to: .released))
        }
        XCTAssertEqual(session.state, .partReplaced, "a rejected transition must not change state")
    }

    func testStepsIncompleteWhenCalibrationSkipped() throws {
        var session = RepairSession(component: "biometric")
        for step: RepairState in [.diagnosed, .inRepair, .partReplaced, .paired, .verified] {
            try session.advance(to: step)
        }
        XCTAssertFalse(session.stepsComplete)
    }

    func testFailedIsTerminal() {
        var session = RepairSession(component: "battery")
        session.fail(code: "NON_GENUINE_PART")
        XCTAssertEqual(session.errorCode, "NON_GENUINE_PART")
        XCTAssertThrowsError(try session.advance(to: .diagnosed))
    }

    func testPartAuthenticatorRejectsMalformedSerials() {
        for serial in ["", "GP-", "XX-DIS000001-1", "GP-DIS000001", "GP-BAT000001-x"] {
            XCTAssertFalse(PartAuthenticator.isGenuine(serial, for: "display"), serial)
        }
    }

    func testDiagnosticSummaryGroupsFailuresByReason() {
        let results = [
            TestResult(test: "battery.health", status: .pass, reason: nil, measurements: [:], durationMs: 0.1),
            TestResult(test: "display.touch", status: .fail, reason: "SENSOR_NO_RESPONSE", measurements: [:], durationMs: 0.1),
            TestResult(test: "camera.focus", status: .fail, reason: "NOT_CALIBRATED", measurements: [:], durationMs: 0.1),
            TestResult(test: "camera.flash", status: .fail, reason: "NOT_CALIBRATED", measurements: [:], durationMs: 0.1),
        ]
        let summary = DiagnosticSummary(results: results)
        XCTAssertEqual(summary.total, 4)
        XCTAssertEqual(summary.passed, 1)
        XCTAssertEqual(summary.passRate, 0.25, accuracy: 1e-9)
        XCTAssertEqual(summary.failuresByReason["NOT_CALIBRATED"], ["camera.focus", "camera.flash"])
    }
}
