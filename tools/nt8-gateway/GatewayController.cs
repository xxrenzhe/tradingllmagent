using System;
using System.Collections.Generic;

namespace TradingLlmAgentGateway;

public sealed class GatewayController
{
    private readonly HashSet<string> _idempotencyKeys = new(StringComparer.Ordinal);
    private readonly List<GatewayEvent> _events = new();
    private long _sequence;

    public IReadOnlyList<GatewayEvent> Events => _events;
    public bool ReadOnly { get; private set; } = true;
    public bool SafeMode { get; private set; } = true;

    public GatewayEvent Heartbeat(IReadOnlyList<string> accounts, IReadOnlyList<string> instruments)
    {
        return Append("heartbeat", new
        {
            active_mode = "sim",
            read_only = ReadOnly,
            safe_mode = SafeMode,
            accounts,
            instruments,
        });
    }

    public GatewayEvent ValidateCommand(GatewayCommand command)
    {
        var errors = new List<string>();
        if (!GatewayProtocol.SupportedCommands.Contains(command.Type))
        {
            errors.Add($"unsupported_command:{command.Type}");
        }
        if (string.IsNullOrWhiteSpace(command.CorrelationId))
        {
            errors.Add("missing_correlation_id");
        }
        if (string.IsNullOrWhiteSpace(command.IdempotencyKey))
        {
            errors.Add("missing_idempotency_key");
        }
        if (!string.IsNullOrWhiteSpace(command.Account) && !GatewayProtocol.IsSimAccount(command.Account))
        {
            errors.Add("non_sim_account_blocked");
        }
        if (!_idempotencyKeys.Add(command.IdempotencyKey))
        {
            errors.Add("duplicate_idempotency_key");
        }
        return Append("command_validation", new { command.Type, command.CorrelationId, errors });
    }

    public GatewayEvent ExternalIntervention(string account, string instrument, string orderId, string reason)
    {
        SafeMode = true;
        return Append("external_intervention", new { account, instrument, order_id = orderId, reason });
    }

    private GatewayEvent Append(string eventType, object payload)
    {
        var gatewayEvent = new GatewayEvent(
            GatewayProtocol.SchemaVersion,
            GatewayProtocol.ProtocolVersion,
            eventType,
            ++_sequence,
            payload,
            DateTimeOffset.UtcNow.ToString("O")
        );
        _events.Add(gatewayEvent);
        return gatewayEvent;
    }
}
