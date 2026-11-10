import XCTest
@testable import DiagKit

/// Decodes the golden fixtures in contracts/, the same files the Python suite replays
/// against the live device agent. If the protocol drifts, one side of CI goes red.
final class ContractTests: XCTestCase {
    struct ContractStep: Decodable {
        let method: String
        let params: [String: JSONValue]
    }

    struct Contract: Decodable {
        let name: String
        let model: String
        let setup: [ContractStep]
        let request: DiagRequest
        let response: DiagResponse
        let exact: [String]
    }

    struct WorkflowContract: Decodable {
        let states: [String]
        let transitions: [String: [String]]
        let requiredSteps: [String: [String]]

        enum CodingKeys: String, CodingKey {
            case states, transitions
            case requiredSteps = "required_steps"
        }
    }

    static let contractsDir: URL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()  // DiagKitTests
        .deletingLastPathComponent()  // Tests
        .deletingLastPathComponent()  // DiagKit
        .deletingLastPathComponent()  // swift
        .deletingLastPathComponent()  // repo root
        .appendingPathComponent("contracts")

    func loadContract(_ name: String) throws -> Contract {
        let url = Self.contractsDir.appendingPathComponent("protocol/\(name).json")
        return try JSONDecoder().decode(Contract.self, from: Data(contentsOf: url))
    }

    func testEveryProtocolFixtureDecodes() throws {
        let dir = Self.contractsDir.appendingPathComponent("protocol")
        let files = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
        XCTAssertGreaterThanOrEqual(files.count, 6, "expected the shared fixtures at \(dir.path)")

        for file in files {
            let contract = try JSONDecoder().decode(Contract.self, from: Data(contentsOf: file))
            XCTAssertEqual(contract.response.id, contract.request.id, contract.name)
            XCTAssertTrue((contract.response.result == nil) != (contract.response.error == nil),
                          "\(contract.name): exactly one of result/error must be set")
        }
    }

    func testHelloIsCompatible() throws {
        let hello = try loadContract("hello").response.decodeResult(Hello.self)
        XCTAssertEqual(hello.protocolVersion, supportedProtocolVersion)
        XCTAssertTrue(hello.isCompatible)
        XCTAssertFalse(hello.serial.isEmpty)
    }

    func testPassingDiagnosticDecodesToTypedResult() throws {
        let result = try loadContract("run_test_pass").response.decodeResult(TestResult.self)
        XCTAssertEqual(result.test, "battery.health")
        XCTAssertEqual(result.status, .pass)
        XCTAssertNil(result.reason)
        XCTAssertEqual(result.component, "battery")
        XCTAssertEqual(result.measurements["cycle_count"]?.doubleValue, 120)
        XCTAssertGreaterThan(result.durationMs, 0)
    }

    func testUnpairedPartFailsWithStableReason() throws {
        let contract = try loadContract("run_test_unpaired_part")
        XCTAssertEqual(contract.setup.map(\.method), ["enter_repair_mode", "replace_part"])
        let result = try contract.response.decodeResult(TestResult.self)
        XCTAssertEqual(result.status, .fail)
        XCTAssertEqual(result.reason, "UNPAIRED_PART")
        XCTAssertTrue(result.measurements.isEmpty)
    }

    func testReplacedPartStartsUnpairedAndUncalibrated() throws {
        let part = try loadContract("replace_part_ok").response.decodeResult(PartReplacement.self)
        XCTAssertEqual(part.component, "display")
        XCTAssertFalse(part.paired)
        XCTAssertFalse(part.calibrated)
        XCTAssertTrue(PartAuthenticator.isGenuine(part.serial, for: "display"))
    }

    func testDeviceErrorIsThrownFromDecodeResult() throws {
        let contract = try loadContract("error_non_genuine_part")
        XCTAssertThrowsError(try contract.response.decodeResult(PartReplacement.self)) { error in
            XCTAssertEqual((error as? DiagError)?.code, "NON_GENUINE_PART")
        }
        guard case .string(let serial)? = contract.request.params["part_serial"] else {
            return XCTFail("fixture should carry the rejected serial")
        }
        XCTAssertFalse(PartAuthenticator.isGenuine(serial, for: "display"),
                       "Swift and Python must agree this part is counterfeit")
    }

    func testUnknownMethodError() throws {
        let error = try XCTUnwrap(loadContract("error_unknown_method").response.error)
        XCTAssertEqual(error.code, "UNKNOWN_METHOD")
    }

    func testRequestEncodesAsSingleJSONLineAndRoundTrips() throws {
        let request = DiagRequest(id: 7, method: "run_test", params: ["test_id": .string("display.touch")])
        let line = try request.jsonLine()
        XCTAssertEqual(line.last, 0x0A)
        XCTAssertEqual(line.filter { $0 == 0x0A }.count, 1)
        let decoded = try JSONDecoder().decode(DiagRequest.self, from: line.dropLast())
        XCTAssertEqual(decoded, request)
    }

    func testSwiftStateMachineMatchesSharedWorkflowContract() throws {
        let url = Self.contractsDir.appendingPathComponent("workflow.json")
        let contract = try JSONDecoder().decode(WorkflowContract.self, from: Data(contentsOf: url))

        XCTAssertEqual(RepairState.allCases.map(\.rawValue), contract.states)
        for state in RepairState.allCases {
            let expected = contract.transitions[state.rawValue] ?? []
            let actual = (RepairRules.transitions[state] ?? []).map(\.rawValue).sorted()
            XCTAssertEqual(actual, expected, "transitions from \(state.rawValue)")
        }
        for (component, steps) in contract.requiredSteps {
            XCTAssertEqual(RepairRules.requiredSteps(for: component).map(\.rawValue), steps, component)
        }
    }
}
