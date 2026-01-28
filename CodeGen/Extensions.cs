using System.Runtime.CompilerServices;

namespace ptr727.ProjectTemplate.CodeGen;

internal static partial class LogExtensions
{
    extension(ILogger logger)
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
    }
}
