using System.Runtime.CompilerServices;
using Microsoft.Extensions.Logging;

namespace ptr727.ProjectTemplate.Console;

[System.Diagnostics.CodeAnalysis.SuppressMessage(
    "Design",
    "CA1034:Nested types should not be visible",
    Justification = "https://github.com/dotnet/sdk/issues/51681"
)]
internal static partial class LogExtensions
{
    extension(Serilog.ILogger logger)
    {
        internal bool LogAndPropagate(
            Exception exception,
            [CallerMemberName] string function = "unknown"
        )
        {
            logger.Error(exception, "{Function}", function);
            return false;
        }

        internal bool LogAndHandle(
            Exception exception,
            [CallerMemberName] string function = "unknown"
        )
        {
            logger.Error(exception, "{Function}", function);
            return true;
        }

        internal Serilog.ILogger LogOverrideContext() => logger.ForContext<LogOverride>();
    }

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1812:Avoid uninstantiated internal classes",
        Justification = "TODO"
    )]
    internal sealed class LogOverride;

    extension(Microsoft.Extensions.Logging.ILogger logger)
    {
        internal bool LogAndPropagate(
            Exception exception,
            [CallerMemberName] string function = "unknown"
        )
        {
            LogCatchException(logger, function, exception);
            return false;
        }

        internal bool LogAndHandle(
            Exception exception,
            [CallerMemberName] string function = "unknown"
        )
        {
            LogCatchException(logger, function, exception);
            return true;
        }
    }

    [LoggerMessage(Message = "Exception in {Function}", Level = LogLevel.Error)]
    internal static partial void LogCatchException(
        this Microsoft.Extensions.Logging.ILogger logger,
        string function,
        Exception exception
    );
}
