# Console App Lifetime

This is a reference skeleton for `CODESTYLE.md` "Interrupts in .NET" and "Unhandled Exceptions in .NET". It builds on the .NET 11 line under the fleet's analyzer set with no warnings, and CSharpier and `dotnet format style` report nothing on it. Adapt the command, the tool, and the log sink, and keep the shape.

## Project

The project selects the .NET 11 line, since `ProcessStartInfo.StartDetached` and `KillOnParentExit` need it. It references System.CommandLine, CliWrap, Serilog, and Serilog.Sinks.Console, with their versions in `Directory.Packages.props`. `AllowUnsafeBlocks` is set because the `LibraryImport` source generator requires it.

```xml
<PropertyGroup>
  <OutputType>Exe</OutputType>
  <TargetFramework>net11.0</TargetFramework>
  <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
</PropertyGroup>
```

## The Lifetime Owner

`Exit` is the one exit decision. `Main` calls it last, after the log is flushed, and it also tells a waiting Windows console close handler that cleanup finished. That handler waits whatever stop came before it, and the kill bound is armed once. The token sources are never disposed, which is what the CA1001 suppression records.

```csharp
using System.Runtime.InteropServices;
using Serilog;

namespace ConsoleApp;

[System.Diagnostics.CodeAnalysis.SuppressMessage(
    "Design",
    "CA1001:Types that own disposable fields should be disposable",
    Justification = "A signal handler can still run after its registration is disposed."
)]
internal sealed partial class Lifetime
{
    public const int ErrorExitCode = 1;

    private readonly CancellationTokenSource _stop = new();
    private readonly CancellationTokenSource _kill = new();
    private readonly ManualResetEventSlim _cleanupDone = new();
    private readonly PosixSignalRegistration[] _registrations;
    private int _stopCount;
    private int _killArmed;
    private int _signalNumber;
    private Exception? _fault;

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Reliability",
        "CA2000:Dispose objects before losing scope",
        Justification = "The registrations live until Exit disposes them."
    )]
    public Lifetime()
    {
        AppDomain.CurrentDomain.UnhandledException += OnUnhandledException;
        TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;
        _registrations =
        [
            PosixSignalRegistration.Create(PosixSignal.SIGINT, OnSignal),
            PosixSignalRegistration.Create(PosixSignal.SIGTERM, OnSignal),
            PosixSignalRegistration.Create(PosixSignal.SIGHUP, OnSignal),
        ];
    }

    public TimeSpan KillBound { get; set; } = TimeSpan.FromSeconds(5);

    public CancellationToken StopToken => _stop.Token;

    public CancellationToken KillToken => _kill.Token;

    public bool StopRequested => _stop.IsCancellationRequested;

    public void Fault(Exception exception)
    {
        _ = Interlocked.CompareExchange(ref _fault, exception, null);
        Log.Error(exception, "Stopping on a fault");
        RequestStop();
    }

    public int Exit(int exitCode)
    {
        _cleanupDone.Set();
        if (Volatile.Read(ref _fault) is not null)
        {
            return ErrorExitCode;
        }
        int number = Volatile.Read(ref _signalNumber);
        if (number == 0)
        {
            return exitCode;
        }
        foreach (PosixSignalRegistration registration in _registrations)
        {
            registration.Dispose();
        }
        if (!OperatingSystem.IsWindows() && Environment.ProcessId != 1)
        {
            _ = SetSignalHandler(number, 0);
            _ = Kill(Environment.ProcessId, number);
        }
        return 128 + number;
    }

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1031:Do not catch general exception types",
        Justification = "An exception escaping a signal handler ends the process."
    )]
    private void OnSignal(PosixSignalContext context)
    {
        try
        {
            if (OperatingSystem.IsWindows() && context.Signal == PosixSignal.SIGHUP)
            {
                _ = Interlocked.CompareExchange(ref _signalNumber, SignalNumber(context.Signal), 0);
                Log.Warning("{Signal} received, the console is closing", context.Signal);
                RequestStop();
                _ = _kill.CancelAsync();
                _ = _cleanupDone.Wait(TimeSpan.FromSeconds(4));
                return;
            }
            int count = Interlocked.Increment(ref _stopCount);
            if (count >= 3)
            {
                Log.Warning("{Signal} received a third time, ending now", context.Signal);
                _kill.Cancel();
                return;
            }
            context.Cancel = true;
            if (count == 2)
            {
                Log.Warning("{Signal} received again, killing tools", context.Signal);
                _ = _kill.CancelAsync();
                return;
            }
            _ = Interlocked.CompareExchange(ref _signalNumber, SignalNumber(context.Signal), 0);
            Log.Warning("{Signal} received, stopping", context.Signal);
            RequestStop();
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"Signal handler failed: {exception.Message}");
        }
    }

    private void RequestStop()
    {
        if (Interlocked.Exchange(ref _killArmed, 1) == 0)
        {
            _kill.CancelAfter(KillBound);
        }
        _ = _stop.CancelAsync();
    }

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1031:Do not catch general exception types",
        Justification = "The process aborts after this handler, so a failure here only loses the kill."
    )]
    private void OnUnhandledException(object sender, UnhandledExceptionEventArgs e)
    {
        Console.Error.WriteLine($"Unhandled exception: {e.ExceptionObject}");
        try
        {
            _kill.Cancel();
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"Killing the tools failed: {exception.Message}");
        }
        Thread flush = new(Log.CloseAndFlush) { IsBackground = true };
        flush.Start();
        _ = flush.Join(TimeSpan.FromSeconds(3));
    }

    private static void OnUnobservedTaskException(
        object? sender,
        UnobservedTaskExceptionEventArgs e
    ) => Log.Error(e.Exception, "Unobserved task exception");

    private static int SignalNumber(PosixSignal signal) =>
        signal switch
        {
            PosixSignal.SIGHUP => 1,
            PosixSignal.SIGINT => 2,
            PosixSignal.SIGTERM => 15,
            _ => throw new ArgumentOutOfRangeException(nameof(signal), signal, null),
        };

    [LibraryImport("libc", EntryPoint = "signal")]
    [DefaultDllImportSearchPaths(DllImportSearchPath.SafeDirectories)]
    private static partial nint SetSignalHandler(int signalNumber, nint handler);

    [LibraryImport("libc", EntryPoint = "kill")]
    [DefaultDllImportSearchPaths(DllImportSearchPath.SafeDirectories)]
    private static partial int Kill(int processId, int signalNumber);
}
```

## The Entry Point

The logger is created after the lifetime owner, so a signal before then logs to Serilog's silent default rather than failing. A callback off the main flow, such as a `FileSystemWatcher` event handler, wraps its body in a catch-all that calls `lifetime.Fault(exception)`.

```csharp
using System.CommandLine;
using System.Globalization;
using Serilog;

namespace ConsoleApp;

internal static class Program
{
    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1031:Do not catch general exception types",
        Justification = "The entry point turns any failure into the error exit status."
    )]
    private static async Task<int> Main(string[] args)
    {
        Lifetime lifetime = new();
        int exitCode;
        try
        {
            Log.Logger = new LoggerConfiguration()
                .WriteTo.Console(formatProvider: CultureInfo.InvariantCulture)
                .CreateLogger();
            ParseResult parseResult = CreateRootCommand(lifetime).Parse(args);
            parseResult.InvocationConfiguration.EnableDefaultExceptionHandler = false;
            parseResult.InvocationConfiguration.ProcessTerminationTimeout = null;
            exitCode = await parseResult
                .InvokeAsync(cancellationToken: lifetime.StopToken)
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (lifetime.StopRequested)
        {
            Log.Information("Stopped before the run finished");
            exitCode = Lifetime.ErrorExitCode;
        }
        catch (Exception exception)
        {
            Log.Error(exception, "Run failed");
            exitCode = Lifetime.ErrorExitCode;
        }
        finally
        {
            await Log.CloseAndFlushAsync().ConfigureAwait(false);
        }
        return lifetime.Exit(exitCode);
    }

    private static RootCommand CreateRootCommand(Lifetime lifetime)
    {
        Argument<FileInfo> tool = new("tool") { Description = "The tool to run on each file" };
        Argument<string[]> files = new("files")
        {
            Description = "The files to run the tool on",
            Arity = ArgumentArity.OneOrMore,
        };
        RootCommand root = [with("Runs a tool on each file"), tool, files];
        root.SetAction(
            (parseResult, cancellationToken) =>
                RunAsync(
                    parseResult.GetRequiredValue(tool).FullName,
                    parseResult.GetRequiredValue(files),
                    lifetime.KillToken,
                    cancellationToken
                )
        );
        return root;
    }

    private static async Task<int> RunAsync(
        string tool,
        IReadOnlyList<string> files,
        CancellationToken killToken,
        CancellationToken stopToken
    )
    {
        int succeeded = 0;
        int failed = 0;
        try
        {
            foreach (string file in files)
            {
                if (stopToken.IsCancellationRequested)
                {
                    break;
                }
                ToolOutcome outcome = await ToolRunner
                    .RunAsync(tool, [file], killToken, stopToken)
                    .ConfigureAwait(false);
                Log.Information("{File}: {Outcome}", file, outcome);
                if (outcome == ToolOutcome.Succeeded)
                {
                    succeeded++;
                }
                else if (outcome == ToolOutcome.Failed)
                {
                    failed++;
                }
            }
        }
        finally
        {
            Log.Information(
                "Summary: {Succeeded} succeeded, {Failed} failed, stopped {Stopped}",
                succeeded,
                failed,
                stopToken.IsCancellationRequested
            );
        }
        return failed == 0 ? 0 : Lifetime.ErrorExitCode;
    }
}
```

## The Tool Runner

CliWrap redirects every standard stream of the child on every run. Its `ExecuteAsync` takes the forceful token before the graceful one.

```csharp
using System.Diagnostics;
using CliWrap;
using Serilog;

namespace ConsoleApp;

internal static class ToolRunner
{
    public static async Task<ToolOutcome> RunAsync(
        string tool,
        IReadOnlyList<string> arguments,
        CancellationToken killToken,
        CancellationToken stopToken
    )
    {
        try
        {
            CommandResult result = await Cli.Wrap(tool)
                .WithArguments(arguments)
                .WithStandardOutputPipe(
                    PipeTarget.ToDelegate(line => Log.Information("{Line}", line))
                )
                .WithStandardErrorPipe(PipeTarget.ToDelegate(line => Log.Warning("{Line}", line)))
                .WithValidation(CommandResultValidation.None)
                .ExecuteAsync(ConfigureStartInfo, null, killToken, stopToken);
            return stopToken.IsCancellationRequested ? ToolOutcome.Cancelled
                : result.ExitCode == 0 ? ToolOutcome.Succeeded
                : ToolOutcome.Failed;
        }
        catch (OperationCanceledException)
            when (stopToken.IsCancellationRequested || killToken.IsCancellationRequested)
        {
            return ToolOutcome.Cancelled;
        }
    }

    private static void ConfigureStartInfo(ProcessStartInfo startInfo)
    {
        if (!OperatingSystem.IsWindows())
        {
            startInfo.StartDetached = true;
        }
        if (OperatingSystem.IsLinux() || OperatingSystem.IsWindows())
        {
            startInfo.KillOnParentExit = true;
        }
    }
}
```

```csharp
namespace ConsoleApp;

internal enum ToolOutcome
{
    Succeeded,
    Failed,
    Cancelled,
}
```

## Measured Stop Matrix

A prototype of this skeleton ran on .NET 11 with System.CommandLine and CliWrap, in the .NET 11 SDK container image. Its child was a shell script that logs each signal it receives.

| Stop | Reaches | App result | Child |
| --- | --- | --- | --- |
| SIGINT to the app | App | Died by SIGINT after cleanup | One SIGINT, own session |
| SIGINT to the process group | App only, child detached | Died by SIGINT | One SIGINT |
| SIGINT to the process group, child not detached | App and child | Died by SIGINT | Two SIGINT |
| Ctrl-C typed at a pty, two children | App only | Died by SIGINT | One SIGINT each |
| SIGTERM, SIGHUP to the app | App | Died by that signal | One SIGINT |
| Child ignores SIGINT, bound 3 s | App | Died by SIGINT at 3 s | Killed at the bound |
| Second SIGINT after 1 s, bound 30 s | App | Died by SIGINT at once | Killed at the second |
| Third SIGINT while cleanup hangs | App | Died by SIGINT with no flush | Already killed |
| SIGKILL to the app, `KillOnParentExit` set | App | Killed | None left |
| SIGKILL to the app, not set | App | Killed | Left running |
| SIGQUIT to the app | App | Died by SIGQUIT, core | None left |
| Exception on a pool thread | n/a | Logged, flushed, died by SIGABRT | None left |
| Fault from a callback | n/a | Exit 1 after cleanup | One SIGINT |
| SIGINT to the child only | Child | Tool failure, exit 2 | Exit 130 |
| Calling `bash` loop, SIGINT to the group | App | Loop stopped | One SIGINT |
| `docker stop`, app as PID 1 | PID 1 | Exit 143 in 1.3 s | One SIGINT |
| `docker kill --signal INT` and `HUP`, PID 1 | PID 1 | Exit 130 and 129 | One SIGINT |
| `docker stop` under `--init` | App | Exit 143 | One SIGINT |
| `docker stop`, child ignores SIGINT, PID 1 | PID 1 | Exit 143 in 5.2 s | Killed at the bound |
| Third SIGINT while cleanup hangs, PID 1 | PID 1 | Exit 130 | Already killed |
| Exception on a pool thread, PID 1 | n/a | Logged, flushed, exit 139 | None left |
| Ctrl-C typed at `docker run -it` | PID 1 | Exit 130 | One SIGINT each |

No run covers Windows, so its console close, Ctrl-C, and job object handling come from the .NET sources alone. macOS has no `KillOnParentExit`, so a child there outlives an app that is killed.
