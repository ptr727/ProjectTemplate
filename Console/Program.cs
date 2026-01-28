using System.CommandLine;
using ptr727.ProjectTemplate.Library;

namespace ptr727.ProjectTemplate.Console;

internal sealed class Program(
    CommandLine.Context commandLineContext,
    CancellationToken cancellationToken
)
{
    internal CommandLine.Context GetCommandLineContext() => commandLineContext;

    internal CancellationToken GetCancellationToken() => cancellationToken;

    internal static async Task<int> Main(string[] args)
    {
        // Parse commandline
        (CommandLine commandLine, RootCommand rootCommand) = await CommandLine
            .CreateRootCommandWithCommandLine()
            .ConfigureAwait(false);
        ParseResult parseResult = rootCommand.Parse(args);

        // Bypass startup for help and version commands
        if (CommandLine.BypassStartup(parseResult))
        {
            return await parseResult.InvokeAsync().ConfigureAwait(false);
        }

        // Create logger
        _ = LoggerFactory.Create(commandLine.CreateContext(parseResult).LogOptions);
        Log.Logger.LogOverrideContext().Information("Starting: {Args}", args);

        // Initialize library with logger
        _ = new TemplateLibrary(
            new Options() { Logger = LoggerFactory.CreateLogger(typeof(TemplateLibrary).FullName!) }
        );

        // Invoke command
        return await parseResult.InvokeAsync().ConfigureAwait(false);
    }

    internal async Task<int> ExecuteAsync()
    {
        try
        {
            await Task.Delay(1000, cancellationToken).ConfigureAwait(false);
            return 0;
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
    }
}
