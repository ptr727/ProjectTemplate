namespace ptr727.ProjectTemplate.Tests;

public sealed class SampleTests : SingleInstanceFixture
{
    [Fact]
    public void BasicAssertion_DefaultValue_ShouldPass()
    {
        // Arrange
        int expected = 42;
        int actual = 42;

        // Act & Assert
        actual.Should().Be(expected);
    }

    [Fact]
    public void StringComparison_WithHelloWorld_ShouldPass()
    {
        // Arrange
        string testString = "Hello, World!";

        // Act & Assert
        testString.Should().NotBeEmpty().And.Contain("World").And.StartWith("Hello");
    }

    [Theory]
    [InlineData(1, 2, 3)]
    [InlineData(0, 0, 0)]
    [InlineData(-1, 1, 0)]
    [InlineData(100, 200, 300)]
    public void Addition_WithInputs_ShouldReturnCorrectSum(int a, int b, int expected)
    {
        // Act
        int result = a + b;

        // Assert
        result.Should().Be(expected);
    }

    [Fact]
    public void ExceptionHandling_DivideByZero_ShouldThrowExpectedException()
    {
        // Arrange
        int zero = 0;

        // Act & Assert
        Action act = () =>
        {
            int _ = 1 / zero;
        };
        act.Should().Throw<DivideByZeroException>();
    }

    [Fact]
    public void CollectionTest_WithItems_ShouldValidateCollection()
    {
        // Arrange
        List<string> items = ["apple", "banana", "cherry"];

        // Assert
        items.Should().HaveCount(3).And.Contain("banana").And.NotContain("orange");
    }

    [Fact]
    public async Task AsyncTest_WithDelay_ShouldCompleteSuccessfully()
    {
        // Arrange
        int delay = 10;

        // Act
        await Task.Delay(delay, TestContext.Current.CancellationToken).ConfigureAwait(true);
        string result = await Task.FromResult("success").ConfigureAwait(true);

        // Assert
        result.Should().Be("success");
    }

    [Fact]
    public void NullChecks_WithNullValue_ShouldValidateNullBehavior()
    {
        // Arrange
        string? nullString = null;
        string nonNullString = "test";

        // Assert
        nullString.Should().BeNull();
        nonNullString.Should().NotBeNull();
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void BooleanTest_WithValue_ShouldValidateBooleanValues(bool value)
    {
        // Act & Assert
        if (value)
        {
            value.Should().BeTrue();
        }
        else
        {
            value.Should().BeFalse();
        }
    }
}
