namespace ptr727.ProjectTemplate.Library;

/// <summary>
/// Options used to configure the library.
/// </summary>
public sealed class Options
{
    /// <summary>
    /// Gets the logger used by the library.
    /// </summary>
    public ILogger Logger { get; init; } = NullLogger.Instance;
}

/// <summary>
/// Provides the primary library functionality.
/// </summary>
public sealed class TemplateLibrary(Options? options = null)
{
    /// <summary>
    /// Gets the logger configured for this library instance.
    /// </summary>
    public ILogger Log { get; } = (options?.Logger) ?? NullLogger.Instance;

    /// <summary>
    /// Writes a test log entry.
    /// </summary>
    public void Test() => Log.LogInformation("Test");
}
