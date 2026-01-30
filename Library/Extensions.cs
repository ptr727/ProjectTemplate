using System.Runtime.CompilerServices;

namespace ptr727.ProjectTemplate.Library;

internal static partial class LogExtensions
{
    extension(ILogger logger)
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
        this ILogger logger,
        string function,
        Exception exception
    );

    [LoggerMessage(Message = "{Message}", Level = LogLevel.Information)]
    internal static partial void LogInformation(this ILogger logger, string message);
}
