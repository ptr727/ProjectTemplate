using System.CommandLine;
using System.CommandLine.Parsing;
using Serilog.Events;

namespace ptr727.ProjectTemplate.Console;

internal sealed class CommandLine
{
    internal CommandLine(string[] args)
    {
        Root = CreateRootCommand();
        Result = Root.Parse(args);
    }

    internal sealed class Options
    {
        internal required LoggerFactory.Options LogOptions { get; init; }
        internal required string TestOption { get; init; }
    }

    internal RootCommand Root { get; init; }
    internal ParseResult Result { get; init; }

    internal RootCommand CreateRootCommand()
    {
        // Default root command
        RootCommand rootCommand = new("C# .NET console project")
        {
            // Global options (set Recursive to true to apply to subcommands)
            _logLevelOption,
            _logFileOption,
            _logFileClearOption,
        };
        rootCommand.SetAction(
            (parseResult, cancellationToken) =>
            {
                Program program = new(CreateOptions(parseResult), cancellationToken);
                return program.ExecuteAsync();
            }
        );

        // Sub commands
        rootCommand.Subcommands.Add(CreateTestCommand());

        return rootCommand;
    }

    internal Command CreateTestCommand()
    {
        Command testCommand = new("test", "Test command")
        {
            // Test command options
            _testOption,
        };
        testCommand.SetAction(
            (parseResult, cancellationToken) =>
            {
                Program program = new(CreateOptions(parseResult), cancellationToken);
                return program.ExecuteTestAsync();
            }
        );
        return testCommand;
    }

    internal Options CreateOptions(ParseResult parseResult) =>
        new()
        {
            LogOptions = new LoggerFactory.Options
            {
                Level = parseResult.GetValue(_logLevelOption),
                File = parseResult.GetValue(_logFileOption) ?? string.Empty,
                FileClear = parseResult.GetValue(_logFileClearOption),
            },
            TestOption = parseResult.GetValue(_testOption) ?? string.Empty,
        };

    private readonly Option<LogEventLevel> _logLevelOption = CreateLogLevelOption();
    private readonly Option<string> _logFileOption = CreateLogFileOption();
    private readonly Option<bool> _logFileClearOption = CreateLogFileClearOption();

    private readonly Option<string> _testOption = CreateTestOption();

    private static Option<bool> CreateLogFileClearOption() =>
        new("--logfile-clear", "-c")
        {
            Description = "Clear the log file before writing (default: false).",
            Recursive = true,
        };

    private static Option<LogEventLevel> CreateLogLevelOption() =>
        new("--loglevel", "-l")
        {
            Description = "Set the log level (default: Information).",
            DefaultValueFactory = _ => LogEventLevel.Information,
            Recursive = true,
        };

    private static Option<string> CreateLogFileOption()
    {
        Option<string> option = new("--logfile", "-f")
        {
            Description = "Write logs to the specified file (optional).",
            Recursive = true,
        };
        return option.AcceptLegalFileNamesOnly();
    }

    private static Option<string> CreateTestOption() =>
        new("--test", "-t") { Description = "Test command option (optional)." };

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
