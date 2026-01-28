namespace ptr727.ProjectTemplate.Library;

public sealed class Options
{
    public ILogger Logger { get; init; } = NullLogger.Instance;
}

public sealed class TemplateLibrary(Options? options = null)
{
    public ILogger Log { get; } = (options?.Logger) ?? NullLogger.Instance;

    public void Test() => Log.LogInformation("Test");
}
