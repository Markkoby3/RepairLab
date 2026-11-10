import Foundation

/// Version of the JSON-lines diagnostic protocol this client understands.
public let supportedProtocolVersion = "1.2"

/// One request line sent to a device agent.
public struct DiagRequest: Codable, Equatable, Sendable {
    public let id: Int
    public let method: String
    public let params: [String: JSONValue]

    public init(id: Int, method: String, params: [String: JSONValue] = [:]) {
        self.id = id
        self.method = method
        self.params = params
    }

    /// The request encoded as a single newline-terminated JSON line, ready for the wire.
    public func jsonLine() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        var data = try encoder.encode(self)
        data.append(0x0A)
        return data
    }
}

/// A device-side rejection with a stable machine-readable code (e.g. NON_GENUINE_PART).
public struct DiagError: Codable, Equatable, Error, Sendable {
    public let code: String
    public let message: String
}

/// One response line. Exactly one of `result` or `error` is present.
public struct DiagResponse: Decodable, Equatable, Sendable {
    public let id: Int?
    public let result: JSONValue?
    public let error: DiagError?

    public static func decode(line: Data) throws -> DiagResponse {
        try JSONDecoder().decode(DiagResponse.self, from: line)
    }

    /// Decode `result` into a typed model, or throw the device's error.
    public func decodeResult<T: Decodable>(_ type: T.Type) throws -> T {
        if let error { throw error }
        guard let result else { throw DiagError(code: "EMPTY_RESPONSE", message: "no result or error") }
        let data = try JSONEncoder().encode(result)
        return try JSONDecoder().decode(T.self, from: data)
    }
}

public struct Hello: Codable, Equatable, Sendable {
    public let protocolVersion: String
    public let serial: String

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol"
        case serial
    }

    public var isCompatible: Bool { protocolVersion.split(separator: ".").first == supportedProtocolVersion.split(separator: ".").first }
}

public enum TestStatus: String, Codable, Sendable {
    case pass
    case fail
}

public struct TestResult: Codable, Equatable, Sendable {
    public let test: String
    public let status: TestStatus
    public let reason: String?
    public let measurements: [String: JSONValue]
    public let durationMs: Double

    enum CodingKeys: String, CodingKey {
        case test, status, reason, measurements
        case durationMs = "duration_ms"
    }

    public var component: String { String(test.split(separator: ".").first ?? "") }
}

public struct PartReplacement: Codable, Equatable, Sendable {
    public let component: String
    public let serial: String
    public let paired: Bool
    public let calibrated: Bool
}
