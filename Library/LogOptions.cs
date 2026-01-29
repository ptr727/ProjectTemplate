using System.Threading;

namespace ptr727.ProjectTemplate.Library;

/// <summary>
/// Provides global logging configuration for the library.
/// </summary>
public static class LogOptions
{
    private static ILoggerFactory s_loggerFactory = NullLoggerFactory.Instance;
    private static ILogger s_logger = NullLogger.Instance;

    /// <summary>
    /// Gets or sets the logger factory used to create category loggers.
    /// </summary>
    public static ILoggerFactory LoggerFactory
    {
        get => Volatile.Read(ref s_loggerFactory);
        set => _ = Interlocked.Exchange(ref s_loggerFactory, value ?? NullLoggerFactory.Instance);
    }

    /// <summary>
    /// Gets or sets the global fallback logger.
    /// </summary>
    public static ILogger Logger
    {
        get => Volatile.Read(ref s_logger);
        set => _ = Interlocked.Exchange(ref s_logger, value ?? NullLogger.Instance);
    }

    /// <summary>
    /// Creates a logger for the specified type using the current factory or fallback logger.
    /// </summary>
    /// <typeparam name="T">The type used to derive the logger category.</typeparam>
    /// <returns>The configured logger for the category.</returns>
    public static ILogger CreateLogger<T>() => CreateLogger(typeof(T).FullName ?? typeof(T).Name);

    /// <summary>
    /// Creates a logger for the specified category using the current factory or fallback logger.
    /// </summary>
    /// <param name="categoryName">The category name for the logger.</param>
    /// <returns>The configured logger for the category.</returns>
    public static ILogger CreateLogger(string categoryName)
    {
        ILoggerFactory loggerFactory = LoggerFactory;
        return !ReferenceEquals(loggerFactory, NullLoggerFactory.Instance)
            ? loggerFactory.CreateLogger(categoryName)
            : Logger;
    }

    /// <summary>
    /// Configures the library to use the specified logger factory.
    /// </summary>
    /// <param name="loggerFactory">The factory to use for new loggers.</param>
    public static void SetFactory(ILoggerFactory loggerFactory) => LoggerFactory = loggerFactory;

    /// <summary>
    /// Attempts to configure the library to use the specified logger factory if none is set.
    /// </summary>
    /// <param name="loggerFactory">The factory to use for new loggers.</param>
    /// <returns>
    /// <c>true</c> when the factory was set because no factory was configured; otherwise, <c>false</c>.
    /// </returns>
    public static bool TrySetFactory(ILoggerFactory loggerFactory)
    {
        ILoggerFactory candidate = loggerFactory ?? NullLoggerFactory.Instance;
        ILoggerFactory original = Interlocked.CompareExchange(
            ref s_loggerFactory,
            candidate,
            NullLoggerFactory.Instance
        );

        return ReferenceEquals(original, NullLoggerFactory.Instance);
    }

    /// <summary>
    /// Configures the library to use the specified global logger.
    /// </summary>
    /// <param name="logger">The logger used as the global fallback.</param>
    public static void SetLogger(ILogger logger) => Logger = logger;

    /// <summary>
    /// Attempts to configure the library to use the specified global logger if none is set.
    /// </summary>
    /// <param name="logger">The logger used as the global fallback.</param>
    /// <returns>
    /// <c>true</c> when the logger was set because no logger was configured; otherwise, <c>false</c>.
    /// </returns>
    public static bool TrySetLogger(ILogger logger)
    {
        ILogger candidate = logger ?? NullLogger.Instance;
        ILogger original = Interlocked.CompareExchange(
            ref s_logger,
            candidate,
            NullLogger.Instance
        );

        return ReferenceEquals(original, NullLogger.Instance);
    }
}
