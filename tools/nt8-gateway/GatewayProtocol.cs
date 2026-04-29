using System;
using System.Collections.Generic;

namespace TradingLlmAgentGateway;

public static class GatewayProtocol
{
    public const int SchemaVersion = 1;
    public const string ProtocolVersion = "nt8-gateway.v1";

    public static readonly HashSet<string> SupportedCommands = new(StringComparer.Ordinal)
    {
        "marketOrder",
        "marketBatch",
        "cancelOrders",
        "flatten",
        "flattenBatch",
        "closeQty",
        "bracket",
    };

    public static bool IsSimAccount(string account)
    {
        return account.StartsWith("Sim", StringComparison.OrdinalIgnoreCase)
            || account.StartsWith("Simulation", StringComparison.OrdinalIgnoreCase);
    }
}
