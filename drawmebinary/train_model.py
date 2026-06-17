import tensorflow as tf
from tensorflow.keras.datasets import mnist

# 1. Load the dataset of handwritten numbers
(x_train, y_train), (x_test, y_test) = mnist.load_data()

# 2. Filter the data to ONLY keep the 0s and 1s
train_filter = (y_train == 0) | (y_train == 1)
test_filter = (y_test == 0) | (y_test == 1)

x_train, y_train = x_train[train_filter], y_train[train_filter]
x_test, y_test = x_test[test_filter], y_test[test_filter]

# Normalize the pixel values to be between 0 and 1 (makes training faster)
x_train, x_test = x_train / 255.0, x_test / 255.0

# 3. Build a very simple, lightweight Neural Network
model = tf.keras.models.Sequential([
    tf.keras.layers.Flatten(input_shape=(28, 28)), # Flattens the 28x28 image grid
    tf.keras.layers.Dense(16, activation='relu'),  # A small "thinking" layer
    tf.keras.layers.Dense(1, activation='sigmoid') # 1 output neuron: outputs a probability close to 0 or 1
])

# 4. Compile the model (giving it a way to measure its mistakes)
model.compile(optimizer='adam',
              loss='binary_crossentropy',
              metrics=['accuracy'])

# 5. Train the model locally!
print("Training the model...")
model.fit(x_train, y_train, epochs=3) # 3 passes over the data is plenty for this

# 6. Test it to see how accurate it is
print("\nTesting the model...")
loss, accuracy = model.evaluate(x_test,  y_test, verbose=2)
print(f"Accuracy: {accuracy * 100:.2f}%")
# Save the trained brain to your project folder
model.save('mnist_binary_verifier.keras')