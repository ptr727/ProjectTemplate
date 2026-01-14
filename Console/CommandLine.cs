using System.CommandLine;
using System.CommandLine.Parsing;
using Serilog.Events;

namespace ptr727.ProjectTemplate.Console;

public class CommandLine
{
    public class Context
    {
        public required DirectoryInfo Path { get; init; }
        public required int Threads { get; init; }
        public required bool DryRun { get; init; }
        public required LoggerFactory.Options LogOptions { get; init; }
    }

    public static async Task<(
        CommandLine commandLine,
        RootCommand rootCommand
    )> CreateRootCommandWithCommandLine()
    {
        CommandLine commandLine = new();
        RootCommand rootCommand = new("C# .NET template project")
        {
            commandLine._pathOption,
            commandLine._dryRunOption,
            commandLine._threadsOption,
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

    public Context CreateContext(ParseResult parseResult) =>
        new()
        {
            Path = parseResult.GetValue(_pathOption)!,
            Threads = parseResult.GetValue(_threadsOption),
            DryRun = parseResult.GetValue(_dryRunOption),
            LogOptions = new LoggerFactory.Options
            {
                Level = parseResult.GetValue(_logLevelOption),
                File = parseResult.GetValue(_logFileOption) ?? string.Empty,
                FileClear = parseResult.GetValue(_logFileClearOption),
            },
        };

    private readonly Option<DirectoryInfo> _pathOption = CreatePathOption();
    private readonly Option<bool> _dryRunOption = CreateDryRunOption();
    private readonly Option<int> _threadsOption = CreateThreadsOption();
    private readonly Option<LogEventLevel> _logLevelOption = CreateLogLevelOption();
    private readonly Option<string> _logFileOption = CreateLogFileOption();
    private readonly Option<bool> _logFileClearOption = CreateLogFileClearOption();

    private static Option<DirectoryInfo> CreatePathOption() =>
        new Option<DirectoryInfo>("--path", "-p")
        {
            Description = "The path to process.",
            Required = true,
        }.AcceptExistingOnly();

    private static Option<bool> CreateDryRunOption() =>
        new("--dryrun", "-d")
        {
            Description = "Perform a dry run without making changes (default: false).",
        };

    private static Option<bool> CreateLogFileClearOption() =>
        new("--logfile-clear", "-c")
        {
            Description = "Clear the log file before writing (default: false).",
        };

    private static Option<int> CreateThreadsOption()
    {
        Option<int> option = new("--threads", "-t")
        {
            Description =
                $"Number of parallel threads (default: {Math.Max(Environment.ProcessorCount, 4)}).",
            DefaultValueFactory = _ => Math.Max(Environment.ProcessorCount, 4),
        };

        option.Validators.Add(result =>
        {
            int value = result.GetValue(option);
            if (value <= 0)
            {
                result.AddError("Thread count must be greater than 0.");
            }
            else if (value > Environment.ProcessorCount)
            {
                result.AddError(
                    $"Thread count must be less than or equal to {Environment.ProcessorCount}."
                );
            }
        });

        return option;
    }

    private static Option<LogEventLevel> CreateLogLevelOption() =>
        new("--loglevel", "-l")
        {
            Description = "Set the log level (default: Information).",
            DefaultValueFactory = _ => LogEventLevel.Information,
        };

    private static Option<string> CreateLogFileOption() =>
        new("--logfile", "-f") { Description = "Write logs to the specified file (optional)." };

    public static bool BypassStartup(ParseResult parseResult) =>
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
