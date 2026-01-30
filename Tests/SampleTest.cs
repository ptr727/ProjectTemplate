namespace ptr727.ProjectTemplate.Tests;

public sealed class SampleTests : SingleInstanceFixture
{
    [Fact]
    public void StringComparison_WithHelloWorld_ShouldPass()
    {
        // Arrange
        const string testString = "Hello, World!";

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
}
