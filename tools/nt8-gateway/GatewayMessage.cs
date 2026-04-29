namespace TradingLlmAgentGateway;

public sealed record GatewayCommand(
    string Type,
    string CorrelationId,
    string IdempotencyKey,
    string? Account,
    string? Instrument,
    string? Action,
    int? Qty
);

public sealed record GatewayEvent(
    int SchemaVersion,
    string ProtocolVersion,
    string EventType,
    long Sequence,
    object Payload,
    string CreatedAt
);
