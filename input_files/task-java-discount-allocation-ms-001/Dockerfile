FROM maven:3.8-openjdk-17

WORKDIR /app

# Copy poms first for dependency caching
COPY pom.xml ./pom.xml
COPY pricing-service/pom.xml ./pricing-service/pom.xml
COPY reporting-service/pom.xml ./reporting-service/pom.xml

RUN mvn -q -DskipTests dependency:go-offline

# Copy the rest
COPY . .

# Build once to ensure it compiles
RUN mvn -q -DskipTests package

CMD ["bash"]