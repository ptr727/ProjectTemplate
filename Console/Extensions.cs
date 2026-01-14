using System.Runtime.CompilerServices;

namespace ptr727.ProjectTemplate.Console;

[System.Diagnostics.CodeAnalysis.SuppressMessage(
    "Design",
    "CA1034:Nested types should not be visible",
    Justification = "https://github.com/dotnet/sdk/issues/51681"
)]
public static class LogExtensions
{
    extension(ILogger logger)
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

        public ILogger LogOverrideContext() => logger.ForContext<LogOverride>();
    }

    public class LogOverride;
}
