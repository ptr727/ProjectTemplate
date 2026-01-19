using System.CommandLine;
using System.Runtime.CompilerServices;
using ptr727.ProjectTemplate.Library;

namespace ptr727.ProjectTemplate.Console;

public class Program
{
    public Program(CommandLine.Context commandLineContext, CancellationToken cancellationToken)
    {
        _commandLineContext = commandLineContext;
        _cancellationToken = cancellationToken;
        _parallelOptions = new ParallelOptions()
        {
            MaxDegreeOfParallelism = commandLineContext.Threads,
            CancellationToken = cancellationToken,
        };
        _httpClient = HttpClientFactory.GetHttpClient();
    }

    private readonly CommandLine.Context _commandLineContext;

    public CommandLine.Context GetCommandLineContext() => _commandLineContext;

    private readonly CancellationToken _cancellationToken;

    public CancellationToken GetCancellationToken() => _cancellationToken;

    private readonly ParallelOptions _parallelOptions;

    public ParallelOptions GetParallelOptions() => _parallelOptions;

    private readonly HttpClient _httpClient;

    public HttpClient GetHttpClient() => _httpClient;

    public static async Task<int> Main(string[] args)
    {
        // Parse commandline
        (CommandLine commandLine, RootCommand rootCommand) =
            await CommandLine.CreateRootCommandWithCommandLine();
        ParseResult parseResult = rootCommand.Parse(args);

        // Bypass startup for help and version commands
        if (CommandLine.BypassStartup(parseResult))
        {
            return await parseResult.InvokeAsync();
        }

        // Create logger
        _ = LoggerFactory.Create(commandLine.CreateContext(parseResult).LogOptions);
        Log.Logger.LogOverrideContext().Information("Starting: {Args}", args);

        // Initialize library with logger (demonstration only)
        _ = new TemplateLibrary(
            new Options() { Logger = LoggerFactory.CreateLogger(typeof(TemplateLibrary).FullName!) }
        );

        // Invoke command
        return await parseResult.InvokeAsync();
    }

    public bool IsDryRun([CallerMemberName] string function = "unknown")
    {
        if (GetCommandLineContext().DryRun)
        {
            Log.Verbose("Dry run enabled, skipping action in {Function}.", function);
        }
        return GetCommandLineContext().DryRun;
    }

    public async Task<int> ExecuteAsync()
    {
        try
        {
            ProcessTask processTask = new(this);
            return await processTask.ExecuteAsync();
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
    }
}
