using Serilog.Debugging;
using Serilog.Events;
using Serilog.Sinks.SystemConsole.Themes;

namespace ptr727.ProjectTemplate.Console;

public static class LoggerFactory
{
    public class Options
    {
        public required LogEventLevel Level { get; init; }
        public required string File { get; init; }
        public required bool FileClear { get; init; }
    }

    public static ILogger Create(Options options)
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
}
