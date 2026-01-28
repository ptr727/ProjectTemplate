using System.CommandLine;
using System.CommandLine.Parsing;
using Serilog.Events;

namespace ptr727.ProjectTemplate.Console;

internal sealed class CommandLine
{
    internal sealed class Context
    {
        internal required LoggerFactory.Options LogOptions { get; init; }
    }

    internal static async Task<(
        CommandLine commandLine,
        RootCommand rootCommand
    )> CreateRootCommandWithCommandLine()
    {
        CommandLine commandLine = new();
        RootCommand rootCommand = new("C# .NET console project")
        {
            commandLine._logLevelOption,
            commandLine._logFileOption,
            commandLine._logFileClearOption,
        };
        rootCommand.SetAction(
            (parseResult, cancellationToken) =>
            {
                Program program = new(commandLine.CreateContext(parseResult), cancellationToken);
                return program.ExecuteAsync();
            }
        );

        return (commandLine, rootCommand);
    }

    internal Context CreateContext(ParseResult parseResult) =>
        new()
        {
            LogOptions = new LoggerFactory.Options
            {
                Level = parseResult.GetValue(_logLevelOption),
                File = parseResult.GetValue(_logFileOption) ?? string.Empty,
                FileClear = parseResult.GetValue(_logFileClearOption),
            },
        };

    private readonly Option<LogEventLevel> _logLevelOption = CreateLogLevelOption();
    private readonly Option<string> _logFileOption = CreateLogFileOption();
    private readonly Option<bool> _logFileClearOption = CreateLogFileClearOption();

    private static Option<bool> CreateLogFileClearOption() =>
        new("--logfile-clear", "-c")
        {
            Description = "Clear the log file before writing (default: false).",
        };

    private static Option<LogEventLevel> CreateLogLevelOption() =>
        new("--loglevel", "-l")
        {
            Description = "Set the log level (default: Information).",
            DefaultValueFactory = _ => LogEventLevel.Information,
        };

    private static Option<string> CreateLogFileOption() =>
        new("--logfile", "-f") { Description = "Write logs to the specified file (optional)." };

    internal static bool BypassStartup(ParseResult parseResult) =>
        parseResult.Errors.Count > 0
        || parseResult.CommandResult.Children.Any(symbolResult =>
            symbolResult is OptionResult optionResult
            && s_cliBypassList.Contains(optionResult.Option.Name)
        );

    private static readonly FrozenSet<string> s_cliBypassList = FrozenSet.Create(
        StringComparer.OrdinalIgnoreCase,
        ["--help", "--version"]
    );
}
