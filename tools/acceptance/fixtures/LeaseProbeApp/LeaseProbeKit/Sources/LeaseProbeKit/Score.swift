public struct Score {
    public let value: Int

    public init(value: Int) {
        self.value = value
    }

    public var doubled: Int { value * 2 }
}
