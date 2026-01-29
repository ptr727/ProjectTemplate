using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using ptr727.ProjectTemplate.Library;

namespace ptr727.ProjectTemplate.Tests;

[Collection("Sequential Test Collection")]
public sealed class LoggingTests
{
    public LoggingTests()
    {
        LogOptions.LoggerFactory = NullLoggerFactory.Instance;
        LogOptions.Logger = NullLogger.Instance;
    }

    [Fact]
    public void TrySetFactory_WhenUnset_ShouldReturnTrueAndSet()
    {
        // Arrange
        using TestLoggerFactory factory = new();

        // Act
        bool configured = LogOptions.TrySetFactory(factory);

        // Assert
        configured.Should().BeTrue();
        ReferenceEquals(LogOptions.LoggerFactory, factory).Should().BeTrue();
    }

    [Fact]
    public void TrySetFactory_WhenAlreadySet_ShouldReturnFalseAndKeepOriginal()
    {
        // Arrange
        using TestLoggerFactory originalFactory = new();
        using TestLoggerFactory newFactory = new();
        LogOptions.SetFactory(originalFactory);

        // Act
        bool configured = LogOptions.TrySetFactory(newFactory);

        // Assert
        configured.Should().BeFalse();
        ReferenceEquals(LogOptions.LoggerFactory, originalFactory).Should().BeTrue();
    }

    [Fact]
    public void TrySetLogger_WhenUnset_ShouldReturnTrueAndSet()
    {
        // Arrange
        TestLogger logger = new();

        // Act
        bool configured = LogOptions.TrySetLogger(logger);

        // Assert
        configured.Should().BeTrue();
        ReferenceEquals(LogOptions.Logger, logger).Should().BeTrue();
    }

    [Fact]
    public void TrySetLogger_WhenAlreadySet_ShouldReturnFalseAndKeepOriginal()
    {
        // Arrange
        TestLogger originalLogger = new();
        TestLogger newLogger = new();
        LogOptions.SetLogger(originalLogger);

        // Act
        bool configured = LogOptions.TrySetLogger(newLogger);

        // Assert
        configured.Should().BeFalse();
        ReferenceEquals(LogOptions.Logger, originalLogger).Should().BeTrue();
    }

    [Fact]
    public void CreateLogger_WithFactory_ShouldUseFactory()
    {
        // Arrange
        using TestLoggerFactory factory = new();
        LogOptions.SetFactory(factory);

        // Act
        ILogger logger = LogOptions.CreateLogger("Category");

        // Assert
        ReferenceEquals(logger, factory.Logger).Should().BeTrue();
        factory.LastCategoryName.Should().Be("Category");
    }

    [Fact]
    public void CreateLogger_WithGlobalLogger_ShouldUseGlobalWhenNoFactory()
    {
        // Arrange
        TestLogger logger = new();
        LogOptions.LoggerFactory = NullLoggerFactory.Instance;
        LogOptions.Logger = logger;

        // Act
        ILogger created = LogOptions.CreateLogger("Category");

        // Assert
        ReferenceEquals(created, logger).Should().BeTrue();
    }

    [Fact]
    public void Log_WhenOptionsLoggerProvided_ShouldUseOptionsLogger()
    {
        // Arrange
        TestLogger logger = new();
        Options options = new() { Logger = logger };

        // Act
        TemplateLibrary library = new(options);

        // Assert
        ReferenceEquals(library.Log, logger).Should().BeTrue();
    }

    [Fact]
    public void Log_WhenOptionsLoggerFactoryProvided_ShouldUseFactoryLogger()
    {
        // Arrange
        using TestLoggerFactory factory = new();
        Options options = new() { LoggerFactory = factory };

        // Act
        TemplateLibrary library = new(options);

        // Assert
        ReferenceEquals(library.Log, factory.Logger).Should().BeTrue();
        factory.LastCategoryName.Should().Be(typeof(TemplateLibrary).FullName);
    }

    [Fact]
    public void Static_WithFactory_ShouldLogInformation()
    {
        // Arrange
        using TestLoggerFactory factory = new();
        LogOptions.SetFactory(factory);

        // Act
        StaticTemplateLibrary.Test();

        // Assert
        factory
            .Logger.Entries.Should()
            .Contain(entry => entry.Level == LogLevel.Information && entry.Message == "Test");
    }

    private sealed class TestLoggerFactory : ILoggerFactory
    {
        public string? LastCategoryName { get; private set; }

        public TestLogger Logger { get; } = new();

        public void AddProvider(ILoggerProvider provider) => _ = provider;

        public ILogger CreateLogger(string categoryName)
        {
            LastCategoryName = categoryName;
            return Logger;
        }

        public void Dispose() { }
    }

    private sealed class TestLogger : ILogger
    {
        private readonly List<LogEntry> _entries = [];

        public IReadOnlyList<LogEntry> Entries => _entries;

        public IDisposable BeginScope<TState>(TState state)
            where TState : notnull
        {
            _ = state;
            return NullScope.Instance;
        }

        public bool IsEnabled(LogLevel logLevel) => true;

        public void Log<TState>(
            LogLevel logLevel,
            EventId eventId,
            TState state,
            Exception? exception,
            Func<TState, Exception?, string> formatter
        )
        {
            _ = eventId;
            string message = formatter(state, exception);
            _entries.Add(new LogEntry(logLevel, message));
        }
    }

    private sealed class NullScope : IDisposable
    {
        public static readonly NullScope Instance = new();

        public void Dispose() { }
    }

    private readonly record struct LogEntry(LogLevel Level, string Message);
}
