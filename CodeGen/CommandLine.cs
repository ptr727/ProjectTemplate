using System.CommandLine;
using System.CommandLine.Parsing;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class CommandLine
{
    private readonly Option<DirectoryInfo> _codePathOption = CreateCodePathOption();
    private readonly Option<string> _apiKeyOption = CreateApiKeyOption();
    private readonly Option<string> _runtimeOption = CreateRuntimeOption();

    private static readonly FrozenSet<string> s_cliBypassList = FrozenSet.Create(
        StringComparer.OrdinalIgnoreCase,
        "--help",
        "--version"
    );

    internal CommandLine(string[] args)
    {
        Root = CreateRootCommand();
        Result = Root.Parse(args);
    }

    internal RootCommand Root { get; }
    internal ParseResult Result { get; }

    internal RootCommand CreateRootCommand()
    {
        RootCommand rootCommand = new("C# .NET codegen project")
        {
            _codePathOption,
            _apiKeyOption,
            _runtimeOption,
        };
        rootCommand.SetAction(
            (parseResult, cancellationToken) =>
            {
                Program program = new(CreateOptions(parseResult), cancellationToken);
                return program.ExecuteAsync();
            }
        );

        return rootCommand;
    }

    internal Options CreateOptions(ParseResult parseResult) =>
        new()
        {
            CodePath = parseResult.GetValue(_codePathOption)!,
            ApiKey = parseResult.GetValue(_apiKeyOption) ?? string.Empty,
            Runtime = parseResult.GetValue(_runtimeOption) ?? string.Empty,
        };

    private static Option<DirectoryInfo> CreateCodePathOption()
    {
        Option<DirectoryInfo> option = new("--codepath", "-p")
        {
            Description = "The path to the code generation output directory.",
            Required = true,
        };
        return option.AcceptExistingOnly();
    }

    private static Option<string> CreateApiKeyOption() =>
        new("--apikey", "-a") { Description = "The API key to use (optional).", Required = false };

    // Local-only demo of per-run state; omit for deterministic CI output (the quote alone).
    private static Option<string> CreateRuntimeOption() =>
        new("--runtime", "-r")
        {
            Description =
                "Embed a timestamp in generated content to demonstrate per-run divergence "
                + "('now' for current UtcNow, or an ISO 8601 value); omit for deterministic output.",
            Required = false,
        };

    internal static bool BypassStartup(ParseResult parseResult) =>
        parseResult.Errors.Count > 0
        || parseResult.CommandResult.Children.Any(symbolResult =>
            symbolResult is OptionResult optionResult
            && s_cliBypassList.Contains(optionResult.Option.Name)
        );

    internal sealed class Options
    {
        internal required DirectoryInfo CodePath { get; init; }
        internal required string ApiKey { get; init; }
        internal required string Runtime { get; init; }
    }
}
