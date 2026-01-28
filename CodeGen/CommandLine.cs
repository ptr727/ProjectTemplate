using System.CommandLine;
using System.CommandLine.Parsing;
using System.IO;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class CommandLine
{
    internal sealed class Context
    {
        internal required DirectoryInfo CodePath { get; init; }
        internal required string APIKey { get; init; }
    }

    internal static async Task<(
        CommandLine commandLine,
        RootCommand rootCommand
    )> CreateRootCommandWithCommandLine()
    {
        CommandLine commandLine = new();
        RootCommand rootCommand = new("C# .NET codegen project")
        {
            commandLine._codePathOption,
            commandLine._apiKeyOption,
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
            CodePath = parseResult.GetValue(_codePathOption)!,
            APIKey = parseResult.GetValue(_apiKeyOption) ?? string.Empty,
        };

    private readonly Option<DirectoryInfo> _codePathOption = CreateCodePathOption();
    private readonly Option<string> _apiKeyOption = CreateAPIKeyOption();

    private static Option<DirectoryInfo> CreateCodePathOption()
    {
        Option<DirectoryInfo> option = new("--codepath", "-p")
        {
            Description = "The path to the code generation output directory.",
            Required = true,
        };
        return option.AcceptExistingOnly();
    }

    private static Option<string> CreateAPIKeyOption() =>
        new("--apikey", "-a") { Description = "The API key to use (optional).", Required = false };

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
