using Serilog.Debugging;
using Serilog.Events;
using Serilog.Extensions.Logging;
using Serilog.Sinks.SystemConsole.Themes;

namespace ptr727.ProjectTemplate.Console;

internal static class LoggerFactory
{
    internal sealed class Options
    {
        internal required LogEventLevel Level { get; init; }
        internal required string File { get; init; }
        internal required bool FileClear { get; init; }
    }

    internal static ILogger Create(Options options)
    {
        // Enable Serilog debug output to the console
        SelfLog.Enable(System.Console.Error);
        LoggerConfiguration loggerConfiguration = new LoggerConfiguration()
            .MinimumLevel.Is(options.Level)
            .MinimumLevel.Override(
                typeof(LogExtensions.LogOverride).FullName!,
                LogEventLevel.Verbose
            )
            .Enrich.WithThreadId()
            .WriteTo.Console(
                theme: AnsiConsoleTheme.Code,
                formatProvider: CultureInfo.InvariantCulture
            );

        // Add file sink if logFile is specified
        if (!string.IsNullOrEmpty(options.File))
        {
            if (options.FileClear && File.Exists(options.File))
            {
                File.Delete(options.File);
            }
            _ = loggerConfiguration.WriteTo.File(
                options.File,
                formatProvider: CultureInfo.InvariantCulture
            );
        }

        // Create logger
        Log.Logger = loggerConfiguration.CreateLogger();
        return Log.Logger;
    }

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Reliability",
        "CA2000:Dispose objects before losing scope",
        Justification = "Dispose handled by Serilog"
    )]
    internal static Microsoft.Extensions.Logging.ILogger CreateLogger(string categoryName) =>
        new SerilogLoggerFactory(Log.Logger, dispose: false).CreateLogger(categoryName);
}
