using Serilog.Debugging;
using Serilog.Events;
using Serilog.Extensions.Logging;
using Serilog.Sinks.SystemConsole.Themes;

namespace ptr727.ProjectTemplate.Console;

internal static class LoggerFactory
{
    private static readonly Lazy<SerilogLoggerFactory> s_serilogLoggerFactory = new(() =>
        new SerilogLoggerFactory(Log.Logger, dispose: false)
    );

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

    internal static Microsoft.Extensions.Logging.ILogger CreateLogger(string categoryName) =>
        s_serilogLoggerFactory.Value.CreateLogger(categoryName);

    internal sealed class Options
    {
        internal required LogEventLevel Level { get; init; }
        internal required string File { get; init; }
        internal required bool FileClear { get; init; }
    }
}
