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
        public bool LogAndPropagate(
            Exception exception,
            [CallerMemberName] string function = "unknown"
        )
        {
            logger.Error(exception, "{Function}", function);
            return false;
        }

        public bool LogAndHandle(
            Exception exception,
            [CallerMemberName] string function = "unknown"
        )
        {
            logger.Error(exception, "{Function}", function);
            return true;
        }

        public Serilog.ILogger LogOverrideContext() => logger.ForContext<LogOverride>();
    }

    public class LogOverride;

    extension(Microsoft.Extensions.Logging.ILogger logger)
    {
        public bool LogAndPropagate(
            Exception exception,
            [CallerMemberName] string function = "unknown"
        )
        {
            LogCatchException(logger, function, exception);
            return false;
        }

        public bool LogAndHandle(
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
